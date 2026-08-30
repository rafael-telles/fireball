"""Backend de transcrição via faster-whisper (CTranslate2), local, sem chave
de API. Cobre tanto o chunk ao vivo (transcribe_chunk) quanto o arquivo
inteiro na transcrição final (transcribe_file).
"""

from __future__ import annotations

from typing import Optional

from fireball.backends import BackendUnavailable

try:
    from faster_whisper import WhisperModel

    _import_error: Optional[Exception] = None
except ImportError as exc:
    WhisperModel = None
    _import_error = exc

DEFAULT_MODEL_SIZE = "base"

# Whisper (mesmo com vad_filter=True) alucina texto a partir de ruído de
# fundo/silêncio em chunks curtos, principalmente em modelos pequenos —
# confirmado em teste manual (ruído ambiente de mic virou frase inventada
# com no_speech_prob baixo, mas avg_logprob bem negativo). Os dois filtros
# abaixo cobrem isso.
DEFAULT_NO_SPEECH_PROB_MAX = 0.6
DEFAULT_MIN_AVG_LOGPROB = -0.8


class WhisperBackend:
    name = "whisper"

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        device: str = "cpu",
        compute_type: str = "int8",
        no_speech_prob_max: float = DEFAULT_NO_SPEECH_PROB_MAX,
        min_avg_logprob: float = DEFAULT_MIN_AVG_LOGPROB,
    ):
        if WhisperModel is None:
            raise BackendUnavailable(
                "faster-whisper não instalado. Instale a dependência de transcrição:\n"
                "  pip install -e '.[whisper]'\n"
                f"Erro original: {_import_error}"
            )
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.no_speech_prob_max = no_speech_prob_max
        self.min_avg_logprob = min_avg_logprob

    def transcribe_chunk(self, audio, samplerate: int, language: str) -> Optional[str]:
        segments, _info = self.model.transcribe(
            audio,
            language=language,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(
            s.text.strip()
            for s in segments
            if s.no_speech_prob < self.no_speech_prob_max and s.avg_logprob > self.min_avg_logprob and s.text.strip()
        ).strip()
        return text or None

    def transcribe_file(self, wav_path, language: str) -> list[dict]:
        segments, _info = self.model.transcribe(str(wav_path), language=language, vad_filter=True)
        return [
            {"start": s.start, "end": s.end, "text": s.text.strip()}
            for s in segments
            if s.no_speech_prob < self.no_speech_prob_max and s.avg_logprob > self.min_avg_logprob and s.text.strip()
        ]
