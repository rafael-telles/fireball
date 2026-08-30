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
from fireball.backends import RealtimeBackend
from fireball.vad import Endpointer

SPEAKER_LABELS = {"mic": "Você", "system": "Outros participantes"}
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

    def _process(key: str, utterances: list) -> bool:
        progressed = False
        for utterance in utterances:
            text = backend.transcribe_chunk(utterance.audio, samplerate, language)
            if text:
                _emit(SPEAKER_LABELS[key], text, utterance.start, utterance.end)
            progressed = True
        return progressed

    while True:
        stopping = stop_flag()
        made_progress = False
        for key, reader in readers.items():
            chunk = reader.read_new()
            if chunk is not None and len(chunk) > 0:
                made_progress = True
                _process(key, endpointers[key].push(chunk))

        if stopping:
            for key, ep in endpointers.items():
                _process(key, ep.flush())
            break

        if not made_progress:
            time.sleep(poll_seconds)
