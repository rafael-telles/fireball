"""Camada de arquivos do Fireball: onde cada reunião mora e como lê/escreve seus dados.

Tudo baseado em arquivos, dentro de ~/.fireball (ou $FIREBALL_HOME):

    meetings/<meeting_id>/
        meeting.json          metadados (nome, status, timestamps, pid do motor)
        transcript.ndjson     a transcrição, um segmento por linha. Nasce do tempo
                              real e é reescrita por `fireball finalize` — uma
                              reunião tem uma transcrição só
        summary.md            resumo gerado da transcrição (ver fireball.summary)
        notes.md              notas ao vivo (Claude + usuário)
        checkpoint.json       até onde o stream já foi processado (resiliência)
        engine.log            stdout/stderr do motor de transcrição em background

    meetings.db               catálogo SQLite (FTS) derivado dessas pastas —
                              busca e lista; os arquivos continuam sendo a fonte
                              da verdade
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


def new_meeting_dir(name: Optional[str] = None) -> Path:
    """A pasta da reunião: hora de criação + slug do nome.

    Reunião nasce sem nome (quem nomeia é a IA, depois), e aí o slug é o
    genérico que o `slugify` já devolve para texto vazio. O nome que a IA
    escrever **não** renomeia a pasta: o id é a identidade, e todo caminho já
    gravado aponta pra ele.

    Duas reuniões criadas no mesmo segundo ganham um sufixo. Enquanto todo
    mundo dava nome, o slug variava e a colisão era quase impossível; com o
    nome saindo de cena o slug virou constante, e o `exist_ok=False` — que está
    aqui justamente para nunca escrever numa pasta que já é de outra reunião —
    passaria a estourar na cara de quem só clicou em "iniciar".
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = meetings_root() / f"{stamp}-{slugify(name or '')}"
    path = base
    suffix = 2
    while True:
        try:
            path.mkdir(parents=True, exist_ok=False)
            return path
        except FileExistsError:
            path = base.with_name(f"{base.name}-{suffix}")
            suffix += 1


def meeting_path(meeting_id: str) -> Path:
    path = meetings_root() / meeting_id
    if not path.is_dir():
        raise FileNotFoundError(f"Reunião '{meeting_id}' não encontrada em {meetings_root()}")
    return path


# O cabeçalho de exemplo que o app escrevia no notes.md ao criar a reunião.
# Hoje a nota nasce vazia, mas as pastas antigas continuam com ele — e esse
# texto não é anotação de ninguém: alimentava o prompt do resumo como se fosse
# e era o que aparecia na tela sob "O que eu anotei".
_SEMENTE_NOTA = (
    "_Notas ao vivo tomadas pelo Claude e por você._",
    "## Notas",
)


def is_note_seed(text: str) -> bool:
    """True quando o notes.md é **só** o cabeçalho de exemplo (nada escrito).

    A assinatura que não dá para confundir com anotação é a linha em itálico
    "Notas ao vivo tomadas pelo Claude e por você." — ela veio do app, não de
    gente. Título e "## Notas" sozinhos não bastam para chamar de semente: um
    "# Pauta" escrito à mão não pode virar nota vazia na tela.
    """
    linhas = [linha.strip() for linha in text.splitlines() if linha.strip()]
    if _SEMENTE_NOTA[0] not in linhas:
        return False
    # fora a assinatura, os cabeçalhos e o "## Notas", não pode sobrar texto
    return all(
        linha in _SEMENTE_NOTA or linha.startswith("# ") for linha in linhas
    )


def read_notes_text(meeting_dir: Path) -> str:
    """O notes.md como anotação de verdade: semente de exemplo conta como vazio."""
    path = Path(meeting_dir) / "notes.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    return "" if is_note_seed(text) else text


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
