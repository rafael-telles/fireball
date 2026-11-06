"""Embeddings locais de voz via ONNX, sem PyTorch."""

from __future__ import annotations

from typing import Optional

import numpy as np

from fireball.backends import BackendUnavailable, onnx_session_options

try:
    from speakeronnx import SpeakerEmbedder

    _import_error: Optional[Exception] = None
except ImportError as exc:
    SpeakerEmbedder = None
    _import_error = exc


DEFAULT_MODEL = "wespeaker-resnet34"


class SpeakerEmbeddingBackend:
    name = "speakeronnx"

    def __init__(self, model: str = DEFAULT_MODEL):
        if SpeakerEmbedder is None:
            raise BackendUnavailable(
                "Reconhecimento de voz indisponível. Instale o extra local:\n"
                "  pip install -e '.[voice]'\n"
                f"Erro original: {_import_error}"
            )
        try:
            self.embedder = SpeakerEmbedder(model=model)
        except Exception as exc:  # download/modelo/onnxruntime
            raise BackendUnavailable(f"Não foi possível carregar o modelo de voz '{model}': {exc}") from exc
        self.model = model
        self._quiet_session()

    def _quiet_session(self) -> None:
        """Recria a sessão do speakeronnx sem spin-wait (ver
        onnx_session_options). O pacote não aceita SessionOptions no
        construtor, então trocamos a sessão interna; se a estrutura dele
        mudar, fica a sessão original — mais gulosa, mas funcionando."""
        try:
            import onnxruntime as rt

            old = self.embedder._session
            self.embedder._session = rt.InferenceSession(
                old._model_path,
                sess_options=onnx_session_options(),
                providers=old.get_providers(),
            )
        except Exception:  # noqa: BLE001 — otimização opcional
            pass

    def embed(self, audio: "np.ndarray", samplerate: int = 16000) -> "np.ndarray":
        if samplerate != self.embedder.sample_rate:
            raise BackendUnavailable(
                f"O backend de voz espera {self.embedder.sample_rate} Hz; recebeu {samplerate} Hz."
            )
        samples = np.asarray(audio, dtype=np.float32)
        if len(samples) < samplerate:
            raise ValueError("É necessário pelo menos 1 segundo de fala para extrair a voz.")
        try:
            return self.embedder.embed(samples)
        except Exception as exc:
            raise BackendUnavailable(f"Não foi possível extrair o embedding de voz: {exc}") from exc
