"""Backend de transcrição em lote via API da Groq (Whisper hospedado,
`whisper-large-v3` / `whisper-large-v3-turbo`).

Só implementa BatchBackend — é uma API paga por requisição, então não faz
sentido chamar por "fala" no loop ao vivo (isso geraria uma requisição a
cada poucos segundos). Use para `fireball finalize --backend groq`, não
para transcrição em tempo real.

Não testado com uma chamada real de API nesta máquina (sem GROQ_API_KEY no
ambiente) — a integração foi verificada contra a assinatura real do SDK
`groq` instalado (`groq.resources.audio.transcriptions.Transcriptions.create`),
mas vale confirmar com uma reunião de verdade antes de confiar cegamente.
"""

from __future__ import annotations

import os
from typing import Optional

from fireball.backends import BackendUnavailable

try:
    from groq import Groq

    _import_error: Optional[Exception] = None
except ImportError as exc:
    Groq = None
    _import_error = exc

DEFAULT_MODEL = "whisper-large-v3-turbo"


class GroqBackend:
    name = "groq"

    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None):
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
        if segments:
            return [
                {
                    "start": seg.get("start") if isinstance(seg, dict) else seg.start,
                    "end": seg.get("end") if isinstance(seg, dict) else seg.end,
                    "text": (seg.get("text") if isinstance(seg, dict) else seg.text).strip(),
                }
                for seg in segments
                if (seg.get("text") if isinstance(seg, dict) else seg.text).strip()
            ]

        text = (result.text or "").strip()
        return [{"start": None, "end": None, "text": text}] if text else []
