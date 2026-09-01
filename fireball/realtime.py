"""Loop de transcrição em tempo real, backend-agnóstico.

Lê os PCMs de mic/system incrementalmente, agrupa o áudio em "falas" via VAD
(fireball.vad.Endpointer) e manda cada fala pronta para o backend escolhido
(ver fireball.backends) — troca-se de backend (whisper, parakeet, ...) sem
mexer neste loop.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import numpy as np

from fireball import storage
from fireball.backends import BackendUnavailable, RealtimeBackend
from fireball.vad import Endpointer

SPEAKER_LABELS = {"mic": "Você", "system": "Outros participantes"}
DIARIZED_PREFIXES = {"mic": "Sala", "system": "Remoto"}
DEFAULT_LANGUAGE = "pt"


class TrackReader:
    """Lê incrementalmente um PCM cru (int16) que está crescendo, devolvendo
    float32 normalizado, sem duplicar nem perder bytes."""

    def __init__(self, pcm_path: Path):
        self.pcm_path = pcm_path
        self._offset = 0

    def read_new(self) -> "np.ndarray | None":
        if not self.pcm_path.exists():
            return None
        size = self.pcm_path.stat().st_size
        available = size - self._offset
        available -= available % 2  # amostras int16 = 2 bytes
        if available <= 0:
            return None
        with open(self.pcm_path, "rb") as f:
            f.seek(self._offset)
            data = f.read(available)
        self._offset += len(data)
        return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def run_realtime_transcription(
    meeting_dir: Path,
    stop_flag: Callable[[], bool],
    backend: RealtimeBackend,
    diarization_backend=None,
    samplerate: int = 16000,
    language: str = DEFAULT_LANGUAGE,
    poll_seconds: float = 0.3,
) -> None:
    """Roda até stop_flag() virar True e as duas tracks ficarem drenadas.

    Pensado para rodar em uma thread dentro do processo do engine, em
    paralelo à gravação (ver engine.run_real_engine).
    """
    transcript_path = meeting_dir / "transcript.ndjson"
    seq_path = meeting_dir / ".transcribe_seq"
    seq = int(seq_path.read_text()) if seq_path.exists() else 0

    readers = {key: TrackReader(meeting_dir / f"{key}.pcm") for key in SPEAKER_LABELS}
    endpointers = {key: Endpointer(samplerate=samplerate) for key in SPEAKER_LABELS}
    try:
        diarization_streams = (
            {key: diarization_backend.open_stream() for key in SPEAKER_LABELS}
            if diarization_backend is not None
            else None
        )
    except BackendUnavailable as exc:
        (meeting_dir / "diarization_warnings.log").write_text(str(exc) + "\n")
        if diarization_backend is not None:
            diarization_backend.close()
        diarization_backend = None
        diarization_streams = None
    pending = {key: [] for key in SPEAKER_LABELS}

    def _emit(speaker: str, text: str, start: float, end: float) -> None:
        nonlocal seq
        seq += 1
        storage.append_ndjson(
            transcript_path,
            {
                "seq": seq,
                "ts": storage.now_iso(),
                # posição da fala dentro da gravação, em segundos. O `ts` diz
                # quando aconteceu no relógio; só isto diz *onde está no
                # áudio*, que é o que o player precisa para acompanhar a
                # transcrição. As duas tracks começam juntas em 0, então o
                # deslocamento vale igual no meeting.wav (a mistura das duas).
                "start": round(start, 2),
                "end": round(end, 2),
                "speaker": speaker,
                "text": text,
                "source": "realtime",
            },
        )
        seq_path.write_text(str(seq))

    def _transcribe(key: str, utterance, turns: list[dict]) -> None:
        if not turns:
            text = backend.transcribe_chunk(utterance.audio, samplerate, language)
            if text:
                _emit(SPEAKER_LABELS[key], text, utterance.start, utterance.end)
            return

        for turn in turns:
            start = max(utterance.start, float(turn["start"]))
            end = min(utterance.end, float(turn["end"]))
            if end - start < 0.2:
                continue
            first = int((start - utterance.start) * samplerate)
            last = int((end - utterance.start) * samplerate)
            text = backend.transcribe_chunk(utterance.audio[first:last], samplerate, language)
            if text:
                speaker = f"{DIARIZED_PREFIXES[key]} {int(turn['speaker_id']) + 1}"
                _emit(speaker, text, start, end)

    def _process(key: str, utterances: list, force: bool = False) -> bool:
        progressed = False
        pending[key].extend(utterances)
        stream = diarization_streams[key] if diarization_streams is not None else None
        while pending[key]:
            utterance = pending[key][0]
            # O Sortformer rotula em blocos de 1,6 s e pode ficar alguns frames
            # atrás da captura. Seguramos a fala até o modelo alcançar seu fim,
            # em vez de publicá-la cedo com o locutor errado.
            if stream is not None and not force and stream.labeled_until + 0.01 < utterance.end:
                break
            turns = stream.turns(utterance.start, utterance.end) if stream is not None else []
            pending[key].pop(0)
            _transcribe(key, utterance, turns)
            progressed = True
        return progressed

    def _disable_diarization(exc: BackendUnavailable) -> None:
        nonlocal diarization_backend, diarization_streams
        (meeting_dir / "diarization_warnings.log").write_text(str(exc) + "\n")
        if diarization_backend is not None:
            diarization_backend.close()
        diarization_backend = None
        diarization_streams = None

    try:
        while True:
            stopping = stop_flag()
            made_progress = False
            for key, reader in readers.items():
                chunk = reader.read_new()
                if chunk is not None and len(chunk) > 0:
                    made_progress = True
                    if diarization_streams is not None:
                        try:
                            diarization_streams[key].push(chunk, samplerate)
                        except BackendUnavailable as exc:
                            _disable_diarization(exc)
                    utterances = endpointers[key].push(chunk)
                    try:
                        _process(key, utterances)
                    except BackendUnavailable as exc:
                        _disable_diarization(exc)
                        _process(key, [])
                elif pending[key]:
                    try:
                        _process(key, [])
                    except BackendUnavailable as exc:
                        _disable_diarization(exc)
                        _process(key, [])

            if stopping:
                if diarization_streams is not None:
                    try:
                        for stream in diarization_streams.values():
                            stream.finish()
                    except BackendUnavailable as exc:
                        _disable_diarization(exc)
                for key, ep in endpointers.items():
                    _process(key, ep.flush(), force=True)
                break

            if not made_progress:
                time.sleep(poll_seconds)
    finally:
        if diarization_backend is not None:
            diarization_backend.close()
