"""Backend de transcrição em lote via API da Groq (Whisper hospedado,
`whisper-large-v3` / `whisper-large-v3-turbo`).

Só implementa BatchBackend — é uma API paga por requisição, então não faz
sentido chamar por "fala" no loop ao vivo (isso geraria uma requisição a
cada poucos segundos). Use para `fireball finalize --backend groq`, não
para transcrição em tempo real.

Testado com uma chamada real de API: em ~72s de áudio quase todo silencioso
(track "system" de um teste manual), o modelo alucinou a mesma frase curta
("E aí") 3 vezes, exatamente a cada 30s (o tamanho da janela interna do
Whisper) — com no_speech_prob=0 e avg_logprob=-0.29, ou seja, "confiante".
As métricas de confiança do próprio Whisper não pegam esse padrão (é
diferente da alucinação em ruído de fundo que o backend whisper local já
filtra). Por isso, aqui filtramos direto pela energia (RMS) do trecho de
áudio correspondente ao segmento — 3 segmentos com RMS exatamente 0.0
confirmaram o diagnóstico.
"""

from __future__ import annotations

import array
import math
import os
import wave
from pathlib import Path
from typing import Optional

from fireball.backends import BackendUnavailable

try:
    from groq import Groq

    _import_error: Optional[Exception] = None
except ImportError as exc:
    Groq = None
    _import_error = exc

DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_MIN_RMS = 0.005


def _field(seg, name: str):
    return seg.get(name) if isinstance(seg, dict) else getattr(seg, name)


def _segment_rms(wav_path: Path, start: Optional[float], end: Optional[float]) -> float:
    """RMS (0..1) do trecho [start, end) do wav. Sem start/end, devolve 1.0
    (não dá pra checar — deixa passar em vez de descartar às cegas)."""
    if start is None or end is None:
        return 1.0
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        start_frame = max(0, int(start * rate))
        end_frame = min(wf.getnframes(), int(end * rate))
        if end_frame <= start_frame:
            return 0.0
        wf.setpos(start_frame)
        frames = wf.readframes(end_frame - start_frame)
    samples = array.array("h", frames)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class GroqBackend:
    name = "groq"

    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None, min_rms: float = DEFAULT_MIN_RMS):
        if Groq is None:
            raise BackendUnavailable(
                "SDK da Groq não instalado. Instale a dependência:\n"
                "  pip install -e '.[groq]'\n"
                f"Erro original: {_import_error}"
            )
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise BackendUnavailable(
                "GROQ_API_KEY não definida. Exporte a variável de ambiente com sua chave da Groq\n"
                "(https://console.groq.com/keys) antes de usar --backend groq."
            )
        self.client = Groq(api_key=key)
        self.model = model
        self.min_rms = min_rms

    def transcribe_file(self, wav_path, language: str) -> list[dict]:
        result = self.client.audio.transcriptions.create(
            file=wav_path,
            model=self.model,
            language=language,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

        # `Transcription` só declara `text` no tipo, mas o SDK aceita campos
        # extras (extra="allow") — segments/language/duration do
        # verbose_json chegam ali mesmo sem estar no schema tipado.
        segments = getattr(result, "segments", None)
        if not segments:
            text = (result.text or "").strip()
            return [{"start": None, "end": None, "text": text}] if text else []

        out = []
        for seg in segments:
            text = (_field(seg, "text") or "").strip()
            if not text:
                continue
            start, end = _field(seg, "start"), _field(seg, "end")
            if _segment_rms(Path(wav_path), start, end) < self.min_rms:
                continue  # confiante mas alucinado — trecho é silêncio de verdade
            out.append({"start": start, "end": end, "text": text})
        return out
