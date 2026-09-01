"""Agenda pelo `gog` (gogcli) instalado na máquina.

Mesma jogada do `claude_code`: delegar a credencial a um binário que a
pessoa já autenticou. O Fireball não guarda client_id, client_secret nem
refresh token — nada disso passa a existir em `~/.fireball/`.

Contrato conferido no gog 0.9.0 (flags que só existem em versões novas —
`--readonly`, `gog schema` — ficam de fora):

    gog calendar events --json --no-input [--account EMAIL] \\
        --from RFC3339 --to RFC3339 --max N

Sucesso: exit 0, stdout `{"events": [...], "nextPageToken": "..."}`.
Cada evento é o Event v3 do Google mais campos que o gogcli acrescenta
(`startLocal`, `endLocal`, `timezone`, …).

Falha: exit ≠ 0, mensagem em texto puro no stderr (não JSON). Dois casos
que a gente reconhece pelo nome porque a pessoa precisa resolver:

- exit 2 / "missing --account" → falta dizer qual conta
- exit 1 / `invalid_grant` → token morto; saída é `gog auth add <email>`
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from typing import Optional

from fireball import settings
from fireball.calendars import CalendarFailed, CalendarUnavailable

TIMEOUT_S = 30
# Janela pedida ao gog pode ser maior que o que a tela mostra; o daemon
# corta no `limit`. Pedir de mais e filtrar aqui evita uma segunda chamada
# quando o calendário tem eventos all-day / cancelados no meio.
FETCH_PAD = 4


class GogCalendar:
    name = "gog"

    def upcoming(self, since: datetime, until: datetime, limit: int = 8) -> list[dict]:
        binary = shutil.which("gog")
        if not binary:
            raise CalendarUnavailable(
                "O comando 'gog' não foi encontrado no PATH. Instale o gogcli "
                "(https://github.com/steipete/gogcli) e autentique com "
                "`gog auth add <email>`."
            )

        account = (settings.load().get("gog_account") or "").strip()
        cmd = [
            binary,
            "calendar",
            "events",
            "--json",
            "--no-input",
            "--from",
            _rfc3339(since),
            "--to",
            _rfc3339(until),
            "--max",
            str(max(limit + FETCH_PAD, limit)),
        ]
        if account:
            cmd += ["--account", account]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise CalendarUnavailable(
                f"O gog não respondeu em {TIMEOUT_S}s."
            ) from exc

        if proc.returncode != 0:
            raise CalendarUnavailable(_explain_failure(proc, account))

        try:
            payload = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            head = (proc.stdout or proc.stderr or "").strip()[:300]
            raise CalendarFailed(
                f"Resposta inesperada do gog (não era JSON): {head!r}"
            ) from exc

        raw_events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(raw_events, list):
            raise CalendarFailed(
                "O gog devolveu JSON sem a lista 'events'."
            )

        events = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            event = _normalize(raw)
            if event is None:
                continue
            events.append(event)
            if len(events) >= limit:
                break
        return events


def _rfc3339(dt: datetime) -> str:
    """RFC3339 com offset; o gog 0.9.0 aceita isto em --from/--to."""
    if dt.tzinfo is None:
        # caller deveria passar aware; assume local só pra não mentir UTC
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def _explain_failure(proc: subprocess.CompletedProcess, account: str) -> str:
    detail = (proc.stderr or proc.stdout or "").strip() or f"código {proc.returncode}"
    lower = detail.lower()
    if "invalid_grant" in lower or "token has been expired" in lower or "revoked" in lower:
        target = account or "<email>"
        return (
            f"O token do gog expirou ou foi revogado. Rode "
            f"`gog auth add {target}` e tente de novo."
        )
    if "missing --account" in lower or "no account" in lower:
        return (
            "O gog precisa saber qual conta usar. Configure o e-mail na tela "
            "de Agenda, ou rode `gog auth add <email>`."
        )
    return f"O gog falhou: {detail}"


def _normalize(raw: dict) -> Optional[dict]:
    """Evento Google → dict Fireball. None = filtrar fora."""
    if (raw.get("status") or "").lower() == "cancelled":
        return None

    event_type = (raw.get("eventType") or "default").lower()
    if event_type not in ("default", ""):
        return None

    start_info = raw.get("start") or {}
    end_info = raw.get("end") or {}
    start = start_info.get("dateTime") or start_info.get("date")
    end = end_info.get("dateTime") or end_info.get("date")
    all_day = "dateTime" not in start_info and bool(start_info.get("date"))
    if all_day or not start:
        return None

    event_id = raw.get("id")
    if not event_id:
        return None

    attendees = []
    for person in raw.get("attendees") or []:
        if not isinstance(person, dict):
            continue
        email = (person.get("email") or "").strip()
        name = (person.get("displayName") or "").strip() or None
        if not email and not name:
            continue
        attendees.append(
            {
                "name": name,
                "email": email or None,
                "self": bool(person.get("self")),
                "organizer": bool(person.get("organizer")),
                "response": person.get("responseStatus") or None,
            }
        )

    organizer = raw.get("organizer") or {}
    organizer_email = (organizer.get("email") or "").strip() or None
    organizer_name = (organizer.get("displayName") or "").strip() or None

    return {
        "id": event_id,
        "calendar_id": raw.get("calendarId") or "primary",
        "provider": "gog",
        "title": (raw.get("summary") or "").strip() or "(sem título)",
        "description": (raw.get("description") or "").strip() or None,
        "location": (raw.get("location") or "").strip() or None,
        "start": start,
        "end": end,
        "all_day": False,
        "attendees": attendees,
        "organizer": (
            {"name": organizer_name, "email": organizer_email}
            if organizer_email or organizer_name
            else None
        ),
        "conference_url": _conference_url(raw),
        "html_link": raw.get("htmlLink") or None,
    }


def _conference_url(raw: dict) -> Optional[str]:
    hangout = (raw.get("hangoutLink") or "").strip()
    if hangout:
        return hangout
    data = raw.get("conferenceData") or {}
    for entry in data.get("entryPoints") or []:
        if not isinstance(entry, dict):
            continue
        uri = (entry.get("uri") or "").strip()
        if uri:
            return uri
    return None
