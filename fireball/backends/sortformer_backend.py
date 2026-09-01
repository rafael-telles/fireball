"""Diarização local com o Sortformer do runtime nativo NeMo-Speech.cpp.

O ASR continua sendo responsabilidade do backend escolhido (Parakeet,
Whisper, Groq). Este módulo só responde "quem falou quando" e devolve turnos
anônimos, que o finalize usa para recortar cada track antes de transcrever.

Usar o CLI oficial evita trazer NeMo/PyTorch para o processo Python e, mais
importante, evita duplicar aqui o pré-processamento e o speaker cache do
Sortformer streaming. O runtime carrega o modelo uma vez para o diretório com
as duas tracks.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fireball import tools
from fireball.backends import BackendUnavailable


DEFAULT_MODEL = os.environ.get("FIREBALL_SORTFORMER_MODEL", "").strip()


class _ModelConfig(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("model_path", ctypes.c_char_p),
        ("gpu", ctypes.c_int32),
        ("preset", ctypes.c_char_p),
        ("chunk_frames", ctypes.c_int32),
        ("right_context_frames", ctypes.c_int32),
        ("left_context_frames", ctypes.c_int32),
        ("fifo_frames", ctypes.c_int32),
        ("spkcache_frames", ctypes.c_int32),
        ("update_period_frames", ctypes.c_int32),
    ]


class _Segment(ctypes.Structure):
    _fields_ = [
        ("start_time", ctypes.c_double),
        ("end_time", ctypes.c_double),
        ("speaker", ctypes.c_int32),
    ]


def _resolve_native_runtime(executable: str, model: str) -> tuple[Path, Path]:
    """Resolve a biblioteca C e o GGUF usados pelo streaming.

    O CLI aceita o nome indexado e baixa o modelo sozinho. A ABI C exige um
    caminho local, então usamos o próprio índice do runtime para resolver (e,
    na primeira execução, baixar) o Sortformer padrão.
    """
    binary = tools.find_binary(executable)
    if not binary:
        raise BackendUnavailable(
            "Diarização ao vivo pedida, mas o runtime 'nemo-speech' não está instalado."
        )

    binary_path = Path(binary).resolve()
    library = binary_path.parent.parent / "lib" / "libnemo_speech_asr_c.so"
    if not library.exists():
        raise BackendUnavailable(f"Biblioteca nativa de diarização não encontrada: {library}")

    if model:
        model_path = Path(model).expanduser()
        if not model_path.is_file():
            raise BackendUnavailable(f"Modelo Sortformer não encontrado: {model_path}")
        return library, model_path

    result = subprocess.run(
        [str(binary_path), "--json", "pull", "sortformer"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "erro desconhecido").strip()
        raise BackendUnavailable(f"Não foi possível preparar o Sortformer: {detail}")
    try:
        artifacts = json.loads(result.stdout)["artifacts"]
        model_path = Path(next(item["path"] for item in artifacts if item["role"] == "diarization"))
    except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise BackendUnavailable("O nemo-speech não informou o caminho do Sortformer.") from exc
    if not model_path.is_file():
        raise BackendUnavailable(f"Modelo Sortformer não encontrado depois do download: {model_path}")
    return library, model_path


class SortformerStream:
    """Um estado Sortformer contínuo para uma única track de áudio."""

    def __init__(self, owner: "StreamingSortformerBackend"):
        self._owner = owner
        self._handle = ctypes.c_void_p()
        owner._check(owner._lib.nemo_speech_diar_stream_open(owner._model, ctypes.byref(self._handle)))
        self._closed = False

    @property
    def labeled_until(self) -> float:
        frames = self._owner._lib.nemo_speech_diar_frame_count(self._handle)
        return max(0.0, frames * self._owner.seconds_per_frame)

    def push(self, audio, samplerate: int) -> None:
        samples = audio.astype("float32", copy=False)
        pointer = samples.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        self._owner._check(
            self._owner._lib.nemo_speech_diar_stream_push_f32(
                self._handle, pointer, len(samples), samplerate
            )
        )

    def finish(self) -> None:
        self._owner._check(self._owner._lib.nemo_speech_diar_stream_finish(self._handle))

    def turns(self, start: float, end: float) -> list[dict]:
        """Devolve os trechos exclusivos que cruzam [start, end)."""
        count = ctypes.c_size_t()
        self._owner._check(
            self._owner._lib.nemo_speech_diar_segments(
                self._handle, None, None, 0, ctypes.byref(count)
            )
        )
        if not count.value:
            return []
        output = (_Segment * count.value)()
        self._owner._check(
            self._owner._lib.nemo_speech_diar_segments(
                self._handle, None, output, count.value, ctypes.byref(count)
            )
        )

        turns: list[dict] = []
        occupied_until = start
        for segment in output[: count.value]:
            turn_start = max(start, segment.start_time, occupied_until)
            turn_end = min(end, segment.end_time)
            if turn_end <= turn_start:
                continue
            speaker_id = segment.speaker - 1
            if (
                turns
                and turns[-1]["speaker_id"] == speaker_id
                and turn_start - turns[-1]["end"] <= 0.15
            ):
                turns[-1]["end"] = turn_end
            else:
                turns.append(
                    {"start": turn_start, "end": turn_end, "speaker_id": speaker_id}
                )
            occupied_until = max(occupied_until, turn_end)
        return turns

    def close(self) -> None:
        if not self._closed:
            self._owner._lib.nemo_speech_diar_stream_close(self._handle)
            self._closed = True


class StreamingSortformerBackend:
    """Sortformer via ABI C estável, com um modelo e um stream por track."""

    name = "sortformer"

    def __init__(self, executable: str = "nemo-speech", model: str = DEFAULT_MODEL):
        library, model_path = _resolve_native_runtime(executable, model)
        try:
            self._lib = ctypes.CDLL(str(library))
        except OSError as exc:
            raise BackendUnavailable(f"Não foi possível carregar {library}: {exc}") from exc
        self._bind()
        self._model = ctypes.c_void_p()
        encoded_model = os.fsencode(model_path)
        config = _ModelConfig(
            size=ctypes.sizeof(_ModelConfig),
            model_path=encoded_model,
            gpu=-1,
            preset=b"streaming",
            chunk_frames=0,
            right_context_frames=0,
            left_context_frames=-1,
            fifo_frames=0,
            spkcache_frames=0,
            update_period_frames=0,
        )
        self._check(self._lib.nemo_speech_diar_create(ctypes.byref(config), ctypes.byref(self._model)))
        self.seconds_per_frame = self._lib.nemo_speech_diar_seconds_per_frame(self._model)
        self._streams: list[SortformerStream] = []

    def _bind(self) -> None:
        lib = self._lib
        lib.nemo_speech_asr_last_error.restype = ctypes.c_char_p
        lib.nemo_speech_diar_create.argtypes = [ctypes.POINTER(_ModelConfig), ctypes.POINTER(ctypes.c_void_p)]
        lib.nemo_speech_diar_create.restype = ctypes.c_int
        lib.nemo_speech_diar_destroy.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_diar_seconds_per_frame.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_diar_seconds_per_frame.restype = ctypes.c_double
        lib.nemo_speech_diar_stream_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.nemo_speech_diar_stream_open.restype = ctypes.c_int
        lib.nemo_speech_diar_stream_push_f32.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.c_int32,
        ]
        lib.nemo_speech_diar_stream_push_f32.restype = ctypes.c_int
        lib.nemo_speech_diar_stream_finish.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_diar_stream_finish.restype = ctypes.c_int
        lib.nemo_speech_diar_stream_close.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_diar_frame_count.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_diar_frame_count.restype = ctypes.c_int64
        lib.nemo_speech_diar_segments.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_Segment),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.nemo_speech_diar_segments.restype = ctypes.c_int

    def _check(self, status: int) -> None:
        if status:
            detail = self._lib.nemo_speech_asr_last_error()
            message = detail.decode(errors="replace") if detail else f"status {status}"
            raise BackendUnavailable(f"Sortformer streaming falhou: {message}")

    def open_stream(self) -> SortformerStream:
        stream = SortformerStream(self)
        self._streams.append(stream)
        return stream

    def close(self) -> None:
        for stream in self._streams:
            stream.close()
        self._streams.clear()
        if self._model:
            self._lib.nemo_speech_diar_destroy(self._model)
            self._model = ctypes.c_void_p()


def parse_rttm(path: Path) -> list[dict]:
    """Converte RTTM em turnos ordenados com ids 0-based por ordem de chegada."""
    raw: list[tuple[float, float, str]] = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[0] != "SPEAKER":
            continue
        try:
            start = float(fields[3])
            duration = float(fields[4])
        except ValueError:
            continue
        if duration <= 0:
            continue
        raw.append((start, start + duration, fields[7]))

    raw.sort(key=lambda item: (item[0], item[1]))
    speaker_ids: dict[str, int] = {}
    turns: list[dict] = []
    occupied_until = 0.0
    for start, end, label in raw:
        # RTTM consegue representar sobreposição, mas a track é mono: mandar o
        # mesmo intervalo duas vezes ao ASR duplicaria as palavras. Fazemos uma
        # visão exclusiva e determinística, preservando quem começou primeiro.
        start = max(start, occupied_until)
        if end <= start:
            continue
        speaker_id = speaker_ids.setdefault(label, len(speaker_ids))
        turn = {"start": round(start, 2), "end": round(end, 2), "speaker_id": speaker_id}
        # O pós-processamento pode devolver trechos adjacentes do mesmo slot.
        # Juntá-los evita uma chamada curta de ASR para cada fronteira de frame.
        if turns and turns[-1]["speaker_id"] == speaker_id and start - turns[-1]["end"] <= 0.15:
            turns[-1]["end"] = round(max(turns[-1]["end"], end), 2)
        else:
            turns.append(turn)
        occupied_until = max(occupied_until, end)
    return turns


class SortformerBackend:
    name = "sortformer"

    def __init__(self, executable: str = "nemo-speech", model: str = DEFAULT_MODEL):
        self.executable = tools.find_binary(executable)
        if not self.executable:
            raise BackendUnavailable(
                "Diarização pedida, mas o runtime 'nemo-speech' não está instalado. "
                "Instale o NeMo-Speech.cpp ou finalize com --no-diarize."
            )
        self.model = model

    def diarize_files(self, tracks: dict[str, Path]) -> dict[str, list[dict]]:
        """Diariza todas as tracks num processo, compartilhando o modelo."""
        if not tracks:
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            for key, source in tracks.items():
                target = input_dir / f"{key}.wav"
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)

            cmd = [
                self.executable,
                "diarize",
                str(input_dir),
                "--format",
                "rttm",
                "--output-dir",
                str(output_dir),
                "--concurrency",
                "2",
                "--preset",
                "offline",
            ]
            if self.model:
                cmd += ["--model", self.model]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "erro desconhecido").strip()
                raise BackendUnavailable(f"Sortformer não conseguiu diarizar as tracks: {detail}")

            diarized: dict[str, list[dict]] = {}
            for key in tracks:
                rttm = output_dir / f"{key}.rttm"
                if not rttm.exists():
                    raise BackendUnavailable(f"Sortformer não produziu a saída esperada: {rttm.name}")
                diarized[key] = parse_rttm(rttm)
            return diarized
