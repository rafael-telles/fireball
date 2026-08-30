"""Camada de arquivos do Fireball: onde cada reunião mora e como lê/escreve seus dados.

Tudo baseado em arquivos, dentro de ~/.fireball (ou $FIREBALL_HOME):

    meetings/<meeting_id>/
        meeting.json          metadados (nome, status, timestamps, pid do motor)
        transcript.ndjson     transcrição em tempo real, um segmento por linha
        transcript_final.ndjson  transcrição final, escrita por `fireball finalize`
        notes.md              notas ao vivo (Claude + usuário)
        actions.json          ações pendentes/aprovadas/rejeitadas/executadas
        checkpoint.json       até onde o stream já foi processado (resiliência)
        engine.log            stdout/stderr do motor de transcrição em background
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


def fireball_home() -> Path:
    home = Path(os.environ.get("FIREBALL_HOME", Path.home() / ".fireball"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def meetings_root() -> Path:
    root = fireball_home() / "meetings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "reuniao"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_meeting_dir(name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = meetings_root() / f"{stamp}-{slugify(name)}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def meeting_path(meeting_id: str) -> Path:
    path = meetings_root() / meeting_id
    if not path.is_dir():
        raise FileNotFoundError(f"Reunião '{meeting_id}' não encontrada em {meetings_root()}")
    return path


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def append_ndjson(path: Path, obj: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_ndjson(path: Path, since_seq: int = 0) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("seq", 0) > since_seq:
                yield obj
