"""Resumo de uma reunião já gravada, a partir da transcrição.

Roda como processo separado supervisionado pelo daemon — igual ao engine e ao
finalize, e pelo mesmo motivo: a chamada ao provedor pode demorar minutos e
falhar de formas que não podem derrubar o dono do estado.

Este módulo **não mexe em meeting.json**: quem escreve status é o daemon, que
observa este processo terminar. Aqui só produzimos `summary.md` e um
`summary_result.json` com a procedência — qual provedor escreveu, quando, e
sobre quantos segmentos.

O nome e as tags que o provedor escreveu saem por esse mesmo
`summary_result.json`, e não direto no meeting.json, pela mesma regra: este é
um processo filho, o dono do estado é o daemon. Ele lê o resultado quando o
job sai com código 0 e aplica na reunião (ver `control.apply_summary_metadata`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fireball import storage
from fireball.summarizers import SummarizerUnavailable, get_summary_provider

def read_transcript(meeting_dir: Path) -> list[dict]:
    """A transcrição da reunião — uma só (o finalize reescreve esta mesma)."""
    return list(storage.read_ndjson(meeting_dir / "transcript.ndjson"))


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
    segments = read_transcript(meeting_dir)
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
    written = get_summary_provider(provider).summarize(
        dialogue, {**meeting, "_dir": str(meeting_dir)}
    )

    (meeting_dir / "summary.md").write_text(written["markdown"] + "\n")
    result = {
        "provider": provider,
        "segments": len(segments),
        "generated_at": storage.now_iso(),
        # o nome só vale como sugestão até o daemon decidir aplicá-lo: uma
        # reunião que já tem nome dado por gente não é renomeada por modelo
        # nenhum (ver `control.apply_summary_metadata`)
        "title": written.get("title"),
        "tags": written.get("tags") or [],
    }
    storage.write_json(meeting_dir / "summary_result.json", result)
    return result
