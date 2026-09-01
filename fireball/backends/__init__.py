"""Backends de transcrição plugáveis: cada um sabe transcrever um pedaço de
áudio (`transcribe_chunk`, usado no loop ao vivo) e/ou um arquivo inteiro
(`transcribe_file`, usado por `fireball finalize`).

Adicionar um backend novo: criar um módulo em fireball/backends/, implementar
RealtimeBackend e/ou BatchBackend, e registrar em REALTIME_BACKENDS /
BATCH_BACKENDS abaixo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol


class BackendUnavailable(RuntimeError):
    """Backend escolhido não está instalado/configurado."""


class RealtimeBackend(Protocol):
    """Transcreve um chunk de áudio já isolado por VAD (uma "fala" entre duas
    pausas de silêncio). Deve devolver None/"" para chunks sem fala real —
    cada backend decide como filtrar ruído/alucinação com os sinais que tiver
    disponíveis."""

    name: str

    def transcribe_chunk(self, audio, samplerate: int, language: str) -> Optional[str]: ...


class BatchBackend(Protocol):
    """Transcreve um arquivo de áudio inteiro (usado na transcrição final,
    mais precisa, depois que a reunião termina)."""

    name: str

    def transcribe_file(self, wav_path, language: str) -> list[dict]: ...


class DiarizationBackend(Protocol):
    """Separa pessoas em uma ou mais tracks, sem transcrever o conteúdo."""

    name: str

    def diarize_files(self, tracks: dict[str, Path]) -> dict[str, list[dict]]: ...


class StreamingDiarizationBackend(Protocol):
    """Mantém o modelo carregado e abre um estado independente por track."""

    name: str

    def open_stream(self): ...

    def close(self) -> None: ...


REALTIME_BACKENDS = ("whisper", "parakeet")
BATCH_BACKENDS = ("whisper", "parakeet", "groq")
DIARIZATION_BACKENDS = ("sortformer",)


def get_realtime_backend(name: str) -> RealtimeBackend:
    if name == "whisper":
        from fireball.backends.whisper_backend import WhisperBackend

        return WhisperBackend()
    if name == "parakeet":
        from fireball.backends.parakeet_backend import ParakeetBackend

        return ParakeetBackend()
    if name == "groq":
        raise BackendUnavailable(
            "'groq' é um backend de lote (API paga por requisição) — não dá pra usar em tempo\n"
            "real, isso geraria uma chamada de API a cada poucos segundos. Use --backend groq\n"
            "só em `fireball finalize`; para transcrição ao vivo use 'whisper' ou 'parakeet'."
        )
    raise BackendUnavailable(f"Backend de transcrição desconhecido: '{name}'. Use {REALTIME_BACKENDS}.")


def get_batch_backend(name: str) -> BatchBackend:
    if name == "whisper":
        from fireball.backends.whisper_backend import WhisperBackend

        return WhisperBackend()
    if name == "parakeet":
        from fireball.backends.parakeet_backend import ParakeetBackend

        return ParakeetBackend()
    if name == "groq":
        from fireball.backends.groq_backend import GroqBackend

        return GroqBackend()
    raise BackendUnavailable(f"Backend de transcrição desconhecido: '{name}'. Use {BATCH_BACKENDS}.")


def get_diarization_backend(name: str = "sortformer") -> DiarizationBackend:
    if name == "sortformer":
        from fireball.backends.sortformer_backend import SortformerBackend

        return SortformerBackend()
    raise BackendUnavailable(f"Backend de diarização desconhecido: '{name}'. Use {DIARIZATION_BACKENDS}.")


def get_streaming_diarization_backend(name: str = "sortformer") -> StreamingDiarizationBackend:
    if name == "sortformer":
        from fireball.backends.sortformer_backend import StreamingSortformerBackend

        return StreamingSortformerBackend()
    raise BackendUnavailable(f"Backend de diarização desconhecido: '{name}'. Use {DIARIZATION_BACKENDS}.")
