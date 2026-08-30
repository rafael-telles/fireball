"""Motor de transcrição em tempo real.

Roda como processo em background, iniciado por `fireball start`, e escreve cada
segmento reconhecido em transcript.ndjson. `fireball transcript follow` apenas
acompanha esse arquivo — quem produz os dados é este módulo.

Só o modo --fake está implementado por enquanto: gera uma transcrição simulada,
para permitir testar o resto do pipeline (CLI, skill, Monitor, notas, ações)
sem depender de microfone, VAD ou chave de API.
"""

from __future__ import annotations

import signal
import time
from pathlib import Path

from fireball import storage

FAKE_SCRIPT = [
    ("Rafael", "Bom dia pessoal, vamos começar a sincronização do projeto Fireball."),
    ("Rafael", "Hoje quero revisar o que já decidimos sobre a arquitetura de skill e tools."),
    ("Ana", "Eu terminei de revisar a política de autonomia, ficou bem clara."),
    ("Rafael", "Ótimo. Precisamos decidir quem vai cuidar da integração com o Groq para a transcrição final."),
    ("Ana", "Posso ficar com isso, mas só consigo começar semana que vem."),
    ("Rafael", "Perfeito, fica como ação: Ana investigar a API do Groq até sexta."),
    ("Rafael", "Outro ponto: precisamos abrir um ticket no Linear para o checkpoint de resiliência do loop ao vivo."),
    ("Ana", "Concordo, isso é importante antes de qualquer demo."),
    ("Rafael", "Decisão: o Claude só age sozinho para tomar notas, o resto vira pendência de aprovação."),
    ("Ana", "Faz sentido, principalmente para ações externas como Slack e Linear."),
    ("Rafael", "Acho que é isso por hoje. Vamos nos falando pelo Slack."),
]


def _emit(transcript_path: Path, seq: int, speaker: str, text: str) -> None:
    storage.append_ndjson(
        transcript_path,
        {
            "seq": seq,
            "ts": storage.now_iso(),
            "speaker": speaker,
            "text": text,
            "source": "realtime",
        },
    )


def run_fake_engine(meeting_dir: Path, interval: float = 3.0) -> None:
    transcript_path = meeting_dir / "transcript.ndjson"

    state = {"running": True}

    def _stop(signum, frame):
        state["running"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    for seq, (speaker, text) in enumerate(FAKE_SCRIPT, start=1):
        if not state["running"]:
            break
        _emit(transcript_path, seq, speaker, text)
        time.sleep(interval)


def run_real_engine(meeting_dir: Path) -> None:
    raise NotImplementedError(
        "Motor real de transcrição ainda não implementado.\n"
        "TODO: capturar áudio (ex.: sounddevice) e alimentar um engine de streaming\n"
        "(ex.: faster-whisper local, ou a API de streaming de um provedor), escrevendo\n"
        "cada segmento reconhecido via storage.append_ndjson(transcript_path, ...).\n"
        "Use --fake por enquanto para testar o restante do pipeline."
    )
