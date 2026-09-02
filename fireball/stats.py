"""Estatísticas de uma reunião, derivadas da transcrição.

Nada aqui é gravado: é tudo recalculado da transcrição atual, que é a única
fonte que sabe quem falou e quando. Guardar o número seria guardar uma cópia
que envelhece a cada fala editada, a cada `finalize` que reescreve o texto.

Silêncio, participação e trocas de turno **precisam de tempo por fala**
(`start`/`end`), e só a transcrição final tem isso — a do tempo real carimba
hora de relógio (`ts`), que diz quando o trecho chegou, não quanto durou. Por
isso `timed` vem na resposta: a tela mostra o que dá para mostrar e diz o que
falta, em vez de inventar um silêncio de zero segundo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fireball import storage

# Abaixo disto é respiro entre frases, não pausa da reunião. Serve para as
# duas coisas que a tela promete: contar "pausas longas" e escolher quais
# aparecem como separador no meio da transcrição.
LONG_PAUSE_S = 20.0

# Quantas fatias o gráfico de silêncio ao longo da reunião tem. Fixo de
# propósito: a barra tem largura fixa na tela, e mais fatia que pixel não
# desenha informação nenhuma.
TIMELINE_BUCKETS = 24


def _seconds(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _spans(segments: list[dict]) -> list[dict]:
    """As falas com tempo, ordenadas e sem sobreposição negativa."""
    spans = []
    for seg in segments:
        start = _seconds(seg.get("start"))
        end = _seconds(seg.get("end"))
        if start is None or end is None or end <= start:
            continue
        spans.append(
            {
                "start": start,
                "end": end,
                "speaker": (seg.get("speaker") or "").strip() or "?",
                "words": len((seg.get("text") or "").split()),
                # o slot de diarização e o perfil de voz vêm junto: é por eles
                # que a ficha da reunião abre o diálogo de cadastrar a voz, e
                # sem isso ela teria os nomes mas não o que clicar
                "speaker_key": seg.get("speaker_key"),
                "voice_profile_id": seg.get("voice_profile_id"),
            }
        )
    spans.sort(key=lambda s: s["start"])
    return spans


def _merged(spans: list[dict]) -> list[tuple[float, float]]:
    """União dos intervalos de fala — duas tracks falando junto é um só
    trecho de "não é silêncio", não o dobro do tempo de reunião."""
    out: list[tuple[float, float]] = []
    for span in spans:
        if out and span["start"] <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], span["end"]))
        else:
            out.append((span["start"], span["end"]))
    return out


def _recorded_seconds(meeting: dict, spans: list[dict]) -> float:
    """Quanto tempo a reunião gravou.

    O relógio manda quando os dois carimbos existem; a transcrição entra como
    reserva para reunião que nunca fechou `ended_at` (quebrou, ou o processo
    morreu antes de escrever).
    """
    started = _parse_iso(meeting.get("started_at"))
    ended = _parse_iso(meeting.get("ended_at"))
    if started and ended:
        wall = (ended - started).total_seconds()
        if wall > 0:
            return wall
    return max((span["end"] for span in spans), default=0.0)


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _gaps(merged: list[tuple[float, float]], total: float) -> list[dict]:
    """Os buracos entre falas — incluindo o antes da primeira e o depois da
    última, que também são reunião gravada sem ninguém falando."""
    gaps = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            gaps.append({"start": cursor, "end": start})
        cursor = max(cursor, end)
    if total > cursor:
        gaps.append({"start": cursor, "end": total})
    for gap in gaps:
        gap["seconds"] = round(gap["end"] - gap["start"], 2)
    return gaps


def _turns(spans: list[dict]) -> int:
    """Trocas de turno: quantas vezes o falante mudou de uma fala para a
    seguinte. Duas falas seguidas da mesma pessoa não contam."""
    changes = 0
    previous = None
    for span in spans:
        if previous is not None and span["speaker"] != previous:
            changes += 1
        previous = span["speaker"]
    return changes


def _by_speaker(spans: list[dict]) -> list[dict]:
    """Tempo de fala por pessoa, mais falante primeiro.

    Por falante o tempo **não** é unido com o dos outros (ao contrário do
    silêncio): quem falou por cima de alguém falou aquele tempo, e somar 105%
    de participação seria mais honesto que esconder a sobreposição.
    """
    totals: dict[str, dict] = {}
    for span in spans:
        entry = totals.setdefault(
            span["speaker"],
            {
                "speaker": span["speaker"],
                "seconds": 0.0,
                "words": 0,
                "turns": 0,
                "speaker_key": span["speaker_key"],
                "voice_profile_id": span["voice_profile_id"],
            },
        )
        entry["seconds"] += span["end"] - span["start"]
        entry["words"] += span["words"]
        entry["turns"] += 1
        entry["speaker_key"] = entry["speaker_key"] or span["speaker_key"]
        entry["voice_profile_id"] = entry["voice_profile_id"] or span["voice_profile_id"]
    out = sorted(totals.values(), key=lambda e: e["seconds"], reverse=True)
    for entry in out:
        entry["seconds"] = round(entry["seconds"], 2)
    return out


def _timeline(spans: list[dict], total: float) -> list[dict]:
    """Fatias iguais da reunião, cada uma com a proporção de fala dentro dela.

    É o que o gráfico desenha: barra alta é gente falando, barra rasa é a
    reunião parada naquele pedaço.
    """
    if total <= 0:
        return []
    merged = _merged(spans)
    width = total / TIMELINE_BUCKETS
    buckets = []
    for index in range(TIMELINE_BUCKETS):
        low = index * width
        high = low + width
        speech = 0.0
        for start, end in merged:
            if end <= low:
                continue
            if start >= high:
                break
            speech += min(end, high) - max(start, low)
        buckets.append(
            {
                "start": round(low, 2),
                "end": round(high, 2),
                "speech": round(speech, 2),
                "ratio": round(speech / width, 4) if width > 0 else 0.0,
            }
        )
    return buckets


def meeting_stats(meeting_dir, segments: Optional[list[dict]] = None) -> dict:
    """Números da reunião a partir da transcrição no disco (ou da que vier)."""
    meeting = storage.read_json(meeting_dir / "meeting.json", {}) or {}
    if segments is None:
        segments = list(storage.read_ndjson(meeting_dir / "transcript.ndjson"))

    spans = _spans(segments)
    recorded = round(_recorded_seconds(meeting, spans), 2)
    words = sum(len((seg.get("text") or "").split()) for seg in segments)

    if not spans:
        return {
            "timed": False,
            "segments": len(segments),
            "words": words,
            "recorded_seconds": recorded,
            "speech_seconds": None,
            "silence_seconds": None,
            "silence_ratio": None,
            "longest_silence": None,
            "turn_changes": None,
            "words_per_minute": round(words / (recorded / 60), 0) if recorded >= 60 else None,
            "speakers": [],
            "long_pauses": [],
            "timeline": [],
        }

    merged = _merged(spans)
    speech = sum(end - start for start, end in merged)
    # o total nunca pode ser menor que a fala: transcrição final que passa do
    # `ended_at` (o backend arredonda o fim do último trecho) faria silêncio
    # negativo, e um silêncio negativo na tela é pior que um zero
    total = max(recorded, merged[-1][1])
    gaps = _gaps(merged, total)
    silence = sum(gap["seconds"] for gap in gaps)
    long_pauses = sorted(
        (gap for gap in gaps if gap["seconds"] >= LONG_PAUSE_S),
        key=lambda gap: gap["seconds"],
        reverse=True,
    )

    speakers = _by_speaker(spans)
    for entry in speakers:
        entry["ratio"] = round(entry["seconds"] / total, 4) if total > 0 else 0.0

    return {
        "timed": True,
        "segments": len(segments),
        "words": words,
        "recorded_seconds": round(total, 2),
        "speech_seconds": round(speech, 2),
        "silence_seconds": round(silence, 2),
        "silence_ratio": round(silence / total, 4) if total > 0 else 0.0,
        "longest_silence": long_pauses[0] if long_pauses else None,
        "turn_changes": _turns(spans),
        "words_per_minute": round(words / (speech / 60), 0) if speech >= 30 else None,
        "speakers": speakers,
        "long_pauses": [
            {"start": gap["start"], "seconds": gap["seconds"]}
            for gap in sorted(long_pauses, key=lambda gap: gap["start"])
        ],
        "timeline": _timeline(spans, total),
    }
