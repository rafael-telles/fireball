"""Backend de transcrição via ONNX (onnx-asr), rodando modelos NeMo Parakeet
localmente sem PyTorch/NeMo toolkit — só onnxruntime, bem mais leve de
instalar que o toolkit completo.

Por padrão usa o fine-tune em pt-BR (TAGARELA) do Parakeet TDT 0.6B v3
(alefiury/parakeet-tdt-0.6b-v3-ptBR-TAGARELA-onnx). Esse checkpoint não faz
parte da lista de nomes conhecidos do onnx-asr, então precisa ser baixado
uma vez com `fireball download-model parakeet` (ou apontado via
FIREBALL_PARAKEET_MODEL_DIR) — carregar direto pelo nome do repositório não
funciona para checkpoints fora da lista curada do pacote.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fireball import storage
from fireball.backends import BackendUnavailable

try:
    import onnx_asr

    _import_error: Optional[Exception] = None
except ImportError as exc:
    onnx_asr = None
    _import_error = exc

DEFAULT_ARCHITECTURE = "nemo-conformer-tdt"
MODEL_REPO = "alefiury/parakeet-tdt-0.6b-v3-ptBR-TAGARELA-onnx"
# Checkouts antigos baixavam o modelo para dentro do repo.
_LEGACY_REPO_DIR = Path(__file__).resolve().parent.parent.parent / ".models" / "parakeet-ptbr"


def default_model_dir() -> Path:
    if env := os.environ.get("FIREBALL_PARAKEET_MODEL_DIR"):
        return Path(env)
    if _LEGACY_REPO_DIR.is_dir():
        return _LEGACY_REPO_DIR
    return storage.fireball_home() / "models" / "parakeet-ptbr"


def download_model(model_dir: Optional[Path] = None) -> Path:
    from huggingface_hub import snapshot_download

    model_dir = model_dir or default_model_dir()
    snapshot_download(repo_id=MODEL_REPO, local_dir=str(model_dir))
    return model_dir


# onnx-asr não expõe no_speech_prob/avg_logprob como o whisper — a única
# defesa contra alucinação em silêncio que temos aqui é o VAD (que já corta
# a fala antes de chegar num "utterance" vazio) e descartar texto vazio.


class ParakeetBackend:
    name = "parakeet"

    def __init__(self, model_dir: Optional[str] = None, architecture: str = DEFAULT_ARCHITECTURE):
        if onnx_asr is None:
            raise BackendUnavailable(
                "onnx-asr não instalado. Instale a dependência de transcrição:\n"
                "  pip install -e '.[parakeet]'\n"
                f"Erro original: {_import_error}"
            )
        model_dir = str(model_dir or default_model_dir())
        if not Path(model_dir).is_dir():
            raise BackendUnavailable(
                f"Modelo Parakeet não encontrado em '{model_dir}'. Baixe uma vez (± 2.4GB) com:\n"
                "  fireball download-model parakeet"
            )
        self.model = onnx_asr.load_model(architecture, model_dir)

    def transcribe_chunk(self, audio, samplerate: int, language: str) -> Optional[str]:
        text = self.model.recognize(audio, sample_rate=samplerate).strip()
        return text or None

    def transcribe_file(self, wav_path, language: str) -> list[dict]:
        text = self.model.recognize(str(wav_path)).strip()
        return [{"start": None, "end": None, "text": text}] if text else []
