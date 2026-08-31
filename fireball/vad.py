"""Segmentação de fala por VAD (Voice Activity Detection).

O loop de transcrição em tempo real usa isso pra agrupar áudio em "falas"
(entre pausas de silêncio) antes de mandar pra qualquer backend — em vez de
cortar em janelas de tamanho fixo, que quebram frase no meio e pioram muito
a qualidade da transcrição (confirmado em teste manual: um trecho de 37s
virou 3 fragmentos soltos com corte fixo de 4s, contra uma transcrição
coerente rodando o VAD sobre o áudio inteiro).

Usa o Silero VAD já empacotado dentro do faster-whisper — nenhum download
extra, e funciona independente de qual backend (whisper, parakeet, ...)
efetivamente transcreve cada fala.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    _import_error: Optional[Exception] = None
except ImportError as exc:
    VadOptions = None
    get_speech_timestamps = None
    _import_error = exc


class VadUnavailable(RuntimeError):
    pass


def _require_vad() -> None:
    if get_speech_timestamps is None:
        raise VadUnavailable(
            "VAD indisponível — o segmentador de fala usa o Silero VAD empacotado no\n"
            "faster-whisper, independente de qual backend de transcrição você escolher:\n"
            "  pip install -e '.[whisper]'\n"
            f"Erro original: {_import_error}"
        )


@dataclass
class Utterance:
    """Uma fala fechada, com onde ela está dentro da track.

    O `audio` vai para o backend; `start`/`end` (segundos desde o começo da
    gravação daquela track) vão para o ndjson, e são o que permite ao player
    acompanhar a transcrição em tempo real dentro do .wav depois. Sem isso o
    segmento só teria hora de relógio, que não diz posição em arquivo nenhum.
    """

    audio: "np.ndarray"
    start: float
    end: float


class Endpointer:
    """Acumula áudio de uma track e devolve "falas" fechadas (com silêncio
    suficiente depois) assim que ficam prontas, sem esperar o fim da
    reunião. Falas muito longas são cortadas em max_speech_duration_s pra
    não segurar a latência indefinidamente."""

    def __init__(
        self,
        samplerate: int = 16000,
        min_silence_duration_ms: int = 600,
        min_speech_duration_ms: int = 200,
        max_speech_duration_s: float = 20.0,
        max_buffer_seconds: float = 30.0,
    ):
        _require_vad()
        self.samplerate = samplerate
        self.vad_options = VadOptions(
            min_silence_duration_ms=min_silence_duration_ms,
            min_speech_duration_ms=min_speech_duration_ms,
            max_speech_duration_s=max_speech_duration_s,
        )
        self._min_silence_samples = int(min_silence_duration_ms * samplerate / 1000)
        self._max_buffer_samples = int(max_buffer_seconds * samplerate)
        self._buffer = np.array([], dtype=np.float32)
        # quantas amostras da track já saíram do buffer. Somado ao índice
        # dentro do buffer, dá a posição absoluta da fala na gravação — que é
        # o que a torna localizável no .wav.
        self._consumed = 0

    def _drop(self, samples: int) -> None:
        """Descarta o começo do buffer mantendo a conta da posição absoluta."""
        samples = min(samples, len(self._buffer))
        self._buffer = self._buffer[samples:]
        self._consumed += samples

    def _utterances(self, ranges: list) -> list[Utterance]:
        return [
            Utterance(
                audio=self._buffer[r["start"] : r["end"]].copy(),
                start=(self._consumed + r["start"]) / self.samplerate,
                end=(self._consumed + r["end"]) / self.samplerate,
            )
            for r in ranges
        ]

    def push(self, audio_chunk: np.ndarray) -> list[Utterance]:
        """Adiciona samples novos ao buffer e devolve as falas já fechadas,
        cada uma com sua posição na track. Pode devolver []."""
        self._buffer = np.concatenate([self._buffer, audio_chunk])

        speech_ranges = get_speech_timestamps(self._buffer, self.vad_options, sampling_rate=self.samplerate)
        if not speech_ranges:
            # sem fala nenhuma detectada ainda — descarta silêncio acumulado,
            # mantendo só uma cauda curta (uma fala pode estar começando).
            tail = self.samplerate
            if len(self._buffer) > tail:
                self._drop(len(self._buffer) - tail)
            return []

        last = speech_ranges[-1]
        trailing_silence = len(self._buffer) - last["end"]
        closed_ranges = speech_ranges if trailing_silence >= self._min_silence_samples else speech_ranges[:-1]

        if not closed_ranges:
            if len(self._buffer) < self._max_buffer_samples:
                return []
            # buffer ficando grande demais sem uma pausa clara — força a
            # fala mais antiga a sair mesmo assim, pra não crescer sem limite.
            closed_ranges = speech_ranges[:1]

        ready = self._utterances(closed_ranges)
        self._drop(closed_ranges[-1]["end"])
        return ready

    def flush(self) -> list[Utterance]:
        """Força a saída do que sobrar no buffer (usado ao encerrar a track)."""
        if len(self._buffer) == 0:
            return []
        speech_ranges = get_speech_timestamps(self._buffer, self.vad_options, sampling_rate=self.samplerate)
        ready = self._utterances(speech_ranges)
        self._drop(len(self._buffer))
        return ready


DEFAULT_BLOCK_GAP_S = 1.5
DEFAULT_MAX_BLOCK_S = 600.0
DEFAULT_BLOCK_PAD_S = 0.2


def speech_blocks(
    audio: "np.ndarray",
    samplerate: int = 16000,
    max_gap_s: float = DEFAULT_BLOCK_GAP_S,
    max_block_s: float = DEFAULT_MAX_BLOCK_S,
    pad_s: float = DEFAULT_BLOCK_PAD_S,
) -> list[tuple[float, float]]:
    """Trechos [início, fim) da track que contêm fala, em segundos.

    Diferente do Endpointer, que isola *cada* fala para o loop ao vivo, aqui
    falas vizinhas viram um bloco só: quebrar apenas onde há silêncio longo
    preserva o contexto de que o Whisper precisa para transcrever bem, e
    mantém o número de chamadas de API proporcional às pausas, não às frases.
    """
    _require_vad()
    ranges = get_speech_timestamps(audio, VadOptions(), sampling_rate=samplerate)
    duration = len(audio) / samplerate

    blocks: list[list[float]] = []
    for r in ranges:
        start, end = r["start"] / samplerate, r["end"] / samplerate
        if blocks and start - blocks[-1][1] <= max_gap_s and end - blocks[-1][0] <= max_block_s:
            blocks[-1][1] = end
        else:
            blocks.append([start, end])

    return [(max(0.0, s - pad_s), min(duration, e + pad_s)) for s, e in blocks]
