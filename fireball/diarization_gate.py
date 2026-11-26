"""Pausa a diarização ao vivo enquanto a track está em silêncio.

O Sortformer custa o mesmo com fala ou sem: medido no ritmo real, uma track
só de silêncio gastou 39% de um núcleo contra 43% com fala. Em reunião de um
lado só (presencial, com o sistema mudo; ou remota, com o microfone mudo)
metade do custo ia para uma track sem ninguém.

Aqui a track só alimenta o modelo enquanto o VAD do loop ao vivo (o mesmo
Endpointer que fecha as falas) vê voz. Em silêncio o áudio é pulado, e cada
pulo fica anotado para converter os tempos do modelo, que só conhece o áudio
que recebeu, de volta para a posição na track. Nada some da gravação nem da
transcrição: o PCM e o ASR continuam recebendo tudo, só o modelo de "quem
fala" deixa de ver o silêncio.

Ao retomar, os últimos segundos pulados vão junto, para o modelo ouvir o
começo da fala que o VAD acabou de detectar. O estado do modelo (quem é quem)
continua o mesmo entre pausas, então "Remoto 2" segue sendo a mesma pessoa.
"""

from __future__ import annotations

import os
from bisect import bisect_right
from typing import Optional

import numpy as np

# Quanto silêncio depois da última voz antes de pausar. Precisa cobrir o
# fechamento da fala pelo Endpointer (600 ms de silêncio) com folga; além
# disso só pausamos quando o modelo já rotulou até o fim da última voz.
DEFAULT_HANGOVER_S = float(os.environ.get("FIREBALL_DIARIZATION_HANGOVER_S", "2.0"))
# Quanto áudio de antes da retomada vai junto para o modelo.
DEFAULT_PREROLL_S = float(os.environ.get("FIREBALL_DIARIZATION_PREROLL_S", "2.0"))
ENABLED = os.environ.get("FIREBALL_DIARIZATION_GATE", "1").strip() not in ("0", "false", "no")


class GatedDiarizationStream:
    """Envolve uma stream de diarização (push/finish/turns/labeled_until),
    falando sempre em segundos da track, não do áudio que o modelo viu."""

    def __init__(
        self,
        inner,
        samplerate: int = 16000,
        hangover_s: float = DEFAULT_HANGOVER_S,
        preroll_s: float = DEFAULT_PREROLL_S,
    ):
        self.inner = inner
        self.samplerate = samplerate
        self.hangover_s = hangover_s
        self._preroll_samples = int(preroll_s * samplerate)
        self._track_samples = 0  # tudo que chegou da track
        self._fed_samples = 0  # tudo que foi para o modelo
        # Trechos contíguos que o modelo recebeu: o i-ésimo começa em
        # _track_starts[i] na track e em _fed_starts[i] no modelo.
        self._track_starts: list[float] = []
        self._fed_starts: list[float] = []
        self._active = False
        self._skipped = np.zeros(0, dtype=np.float32)  # cauda pulada, para o pre-roll
        self.paused_samples = 0  # só para medição

    # -- conversão de tempos -------------------------------------------------

    def _fed_to_track(self, t: float) -> float:
        i = bisect_right(self._fed_starts, t) - 1
        if i < 0:
            return t
        return self._track_starts[i] + (t - self._fed_starts[i])

    def _pieces(self, start: float, end: float) -> list[tuple[float, float, float]]:
        """Partes de [start, end) da track que o modelo viu, como
        (início na track, fim na track, deslocamento track→modelo)."""
        pieces = []
        for i, track_start in enumerate(self._track_starts):
            fed_end = self._fed_starts[i + 1] if i + 1 < len(self._fed_starts) else self._fed_samples / self.samplerate
            track_end = track_start + (fed_end - self._fed_starts[i])
            lo, hi = max(start, track_start), min(end, track_end)
            if hi > lo:
                pieces.append((lo, hi, self._fed_starts[i] - track_start))
        return pieces

    # -- interface de stream ---------------------------------------------------

    @property
    def labeled_until(self) -> float:
        if not self._track_starts:
            # o modelo nunca recebeu nada: a track inteira é silêncio
            return self._track_samples / self.samplerate
        labeled = self._fed_to_track(self.inner.labeled_until)
        if not self._active:
            # pausado só depois de o modelo alcançar a última voz; o resto
            # é silêncio, que não tem locutor para esperar
            labeled = max(labeled, self._track_samples / self.samplerate)
        return labeled

    def push(self, audio, samplerate: int, last_speech_end: Optional[float] = None) -> None:
        """`last_speech_end`: fim da voz mais recente que o VAD viu, em segundos
        da track (None = nenhuma ainda, então segue pausado)."""
        audio = np.asarray(audio, dtype=np.float32)
        chunk_start = self._track_samples
        self._track_samples += len(audio)
        now = self._track_samples / samplerate

        if last_speech_end is None:
            want_active = False
        else:
            recent = now - last_speech_end < self.hangover_s
            # não pausa com fala ainda sem rótulo: ela ficaria presa
            caught_up = (
                self._active is False
                or self._fed_to_track(self.inner.labeled_until) + 0.01 >= last_speech_end
            )
            want_active = recent or not caught_up

        if not want_active:
            self._active = False
            self.paused_samples += len(audio)
            if self._preroll_samples:
                self._skipped = np.concatenate([self._skipped, audio])[-self._preroll_samples :]
            return

        if not self._active:
            preroll = self._skipped
            self._track_starts.append((chunk_start - len(preroll)) / samplerate)
            self._fed_starts.append(self._fed_samples / samplerate)
            self.paused_samples -= len(preroll)
            audio = np.concatenate([preroll, audio])
            self._skipped = np.zeros(0, dtype=np.float32)
            self._active = True

        self.inner.push(audio, samplerate)
        self._fed_samples += len(audio)

    def finish(self) -> None:
        if self._track_starts:
            self.inner.finish()

    def turns(self, start: float, end: float) -> list[dict]:
        result: list[dict] = []
        for lo, hi, offset in self._pieces(start, end):
            for turn in self.inner.turns(lo + offset, hi + offset):
                result.append(
                    {
                        **turn,
                        "start": float(turn["start"]) - offset,
                        "end": float(turn["end"]) - offset,
                    }
                )
        return result

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close is not None:
            close()


class AlwaysOnStream(GatedDiarizationStream):
    """Mesma interface, sem pausa nenhuma (FIREBALL_DIARIZATION_GATE=0)."""

    def push(self, audio, samplerate: int, last_speech_end: Optional[float] = None) -> None:
        super().push(audio, samplerate, last_speech_end=float("inf"))


def wrap(inner, samplerate: int = 16000):
    return GatedDiarizationStream(inner, samplerate) if ENABLED else AlwaysOnStream(inner, samplerate)
