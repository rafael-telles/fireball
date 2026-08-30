"""Resumo de uma reunião já gravada, a partir da transcrição.

Roda como processo separado supervisionado pelo daemon — igual ao engine e ao
finalize, e pelo mesmo motivo: a chamada ao provedor pode demorar minutos e
falhar de formas que não podem derrubar o dono do estado.

Este módulo **não mexe em meeting.json**: quem escreve status é o daemon, que
observa este processo terminar. Aqui só produzimos `summary.md` e um
`summary_result.json` com a procedência — qual provedor escreveu, quando, e
sobre qual das duas transcrições.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fireball import storage
from fireball.summarizers import SummarizerUnavailable, get_summary_provider

# Resumir a transcrição final é sempre melhor: ela roda sobre o áudio inteiro
# e erra menos. A do tempo real é o que sobra quando a reunião nunca foi
# finalizada — resumo de texto pior, mas resumo.
SOURCES = (("final", "transcript_final.ndjson"), ("realtime", "transcript.ndjson"))


def pick_transcript(meeting_dir: Path) -> tuple[Optional[str], list[dict]]:
    """A melhor transcrição disponível: (nome da fonte, segmentos)."""
    for source, filename in SOURCES:
        segments = list(storage.read_ndjson(meeting_dir / filename))
        if segments:
            return source, segments
    return None, []


def as_dialogue(segments: list[dict]) -> str:
    """Uma fala por linha, `Falante: texto` — o formato que o prompt promete.

    Sem carimbo de hora de propósito: o resumo não usa, e cada hora na linha
    seria contexto pago sem retorno.
    """
    lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"{seg.get('speaker') or '?'}: {text}")
    return "\n".join(lines)


def run_summary(meeting_dir: Path, provider: str) -> dict:
    """Gera o resumo e grava `summary.md`. Devolve (e grava) a procedência."""
    meeting = storage.read_json(meeting_dir / "meeting.json")
    source, segments = pick_transcript(meeting_dir)
    if not segments:
        raise SummarizerUnavailable(
            "Esta reunião não tem transcrição nenhuma para resumir."
        )

    dialogue = as_dialogue(segments)
    if not dialogue.strip():
        raise SummarizerUnavailable(
            "A transcrição desta reunião não tem nenhuma fala com texto."
        )

    # o provedor pode querer a pasta (o claude_code roda com o cwd nela)
    markdown = get_summary_provider(provider).summarize(dialogue, {**meeting, "_dir": str(meeting_dir)})

    (meeting_dir / "summary.md").write_text(markdown + "\n")
    result = {
        "provider": provider,
        "source": source,
        "segments": len(segments),
        "generated_at": storage.now_iso(),
    }
    storage.write_json(meeting_dir / "summary_result.json", result)
    return result
