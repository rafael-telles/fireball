"""Backends de transcrição plugáveis: cada um sabe transcrever um pedaço de
áudio (`transcribe_chunk`, usado no loop ao vivo) e/ou um arquivo inteiro
(`transcribe_file`, usado por `fireball finalize`).

Adicionar um backend novo: criar um módulo em fireball/backends/, implementar
RealtimeBackend e/ou BatchBackend, e registrar em REALTIME_BACKENDS /
BATCH_BACKENDS abaixo.
"""

from __future__ import annotations

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


def get_realtime_backend(name: str) -> RealtimeBackend:
    if name == "whisper":
        from fireball.backends.whisper_backend import WhisperBackend

        return WhisperBackend()
    if name == "parakeet":
        from fireball.backends.parakeet_backend import ParakeetBackend

        return ParakeetBackend()
    raise BackendUnavailable(f"Backend de transcrição desconhecido: '{name}'. Use 'whisper' ou 'parakeet'.")


def get_batch_backend(name: str) -> BatchBackend:
    if name == "whisper":
        from fireball.backends.whisper_backend import WhisperBackend

        return WhisperBackend()
    if name == "parakeet":
        from fireball.backends.parakeet_backend import ParakeetBackend

        return ParakeetBackend()
    raise BackendUnavailable(f"Backend de transcrição desconhecido: '{name}'. Use 'whisper' ou 'parakeet'.")
