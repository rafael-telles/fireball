"""Operações de controle de reunião (start/stop/status/list), reutilizadas
tanto pela CLI (`fireball/cli.py`) quanto pela GUI (`fireball/gui/`) — uma
única implementação, sem duplicar lógica entre os dois front-ends.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fireball import realtime, storage


def start_meeting(
    name: str = "Reunião",
    fake: bool = True,
    interval: float = 3.0,
    mic_device: Optional[str] = None,
    system_device: Optional[str] = None,
    transcribe: bool = True,
    backend: str = "whisper",
    language: str = realtime.DEFAULT_LANGUAGE,
) -> dict:
    """Cria a pasta da reunião e sobe o motor de gravação/transcrição em
    background (processo `_engine` separado). Devolve o dict de meeting.json."""
    meeting_dir = storage.new_meeting_dir(name)
    meeting_id = meeting_dir.name

    (meeting_dir / "notes.md").write_text(
        f"# {name}\n\n_Notas ao vivo tomadas pelo Claude e por você._\n\n## Notas\n"
    )
    (meeting_dir / "transcript.ndjson").touch()

    cmd = [
        sys.executable,
        "-m",
        "fireball.cli",
        "_engine",
        meeting_id,
        "--fake" if fake else "--real",
        "--interval",
        str(interval),
    ]
    if mic_device is not None:
        cmd += ["--mic-device", mic_device]
    if system_device is not None:
        cmd += ["--system-device", system_device]
    if not fake:
        cmd += ["--transcribe" if transcribe else "--no-transcribe", "--backend", backend, "--language", language]

    log_path = meeting_dir / "engine.log"
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    meeting = {
        "id": meeting_id,
        "name": name,
        "started_at": storage.now_iso(),
        "ended_at": None,
        "status": "recording",
        "fake": fake,
        "engine_pid": proc.pid,
        "mic_device": mic_device,
        "system_device": system_device,
        "transcribe_live": transcribe if not fake else None,
        "backend": backend if not fake else None,
        "language": language if not fake else None,
    }
    storage.write_json(meeting_dir / "meeting.json", meeting)
    storage.write_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    storage.write_json(meeting_dir / "actions.json", [])
    return meeting


def stop_meeting(meeting_id: str) -> dict:
    """Encerra o motor em background (SIGTERM) e marca a reunião como parada.
    Devolve o dict de meeting.json atualizado."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    pid = meeting.get("engine_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    meeting["status"] = "stopped"
    meeting["ended_at"] = storage.now_iso()
    storage.write_json(meeting_dir / "meeting.json", meeting)
    return meeting


def get_meeting_status(meeting_id: str) -> dict:
    """Metadados + checkpoint + contagem de segmentos/ações pendentes."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    checkpoint = storage.read_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    segments = list(storage.read_ndjson(meeting_dir / "transcript.ndjson"))
    actions = storage.read_json(meeting_dir / "actions.json", [])
    pending = [a for a in actions if a["status"] == "pending"]
    return {
        "meeting": meeting,
        "checkpoint": checkpoint,
        "segments_total": len(segments),
        "actions_pending": len(pending),
    }


def list_meetings() -> list[dict]:
    """Todas as reuniões conhecidas, mais recentes por último (ordem de pasta)."""
    rows = []
    for d in sorted(storage.meetings_root().iterdir()):
        if d.is_dir():
            rows.append(storage.read_json(d / "meeting.json", {"id": d.name}))
    return rows


def active_meeting() -> Optional[dict]:
    """A reunião mais recente ainda com status 'recording', se houver."""
    for meeting in reversed(list_meetings()):
        if meeting.get("status") == "recording":
            return meeting
    return None
