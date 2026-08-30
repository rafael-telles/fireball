"""Protocolo de IPC entre o daemon do Fireball e seus clientes (CLI e GUI).

Uma mensagem = uma linha JSON, sobre um socket Unix. Sem dependência externa,
sem servidor HTTP: o daemon é local, de um usuário só, e o socket já dá
autenticação por permissão de arquivo (0600).

    pedido    {"op": "start", "args": {...}}
    resposta  {"ok": true,  "result": ...}
              {"ok": false, "error": {"kind": "MeetingAlreadyActive", "message": "..."}}

O campo `kind` existe para o cliente reconstruir o *tipo* do erro (e não só o
texto), pra CLI e GUI poderem tratar "já tem reunião rodando" diferente de uma
falha genérica.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from fireball import storage

# sun_path de AF_UNIX no Linux tem 108 bytes; usamos folga.
MAX_SOCKET_PATH = 100

PROTOCOL_VERSION = 1


def _runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg)
    return Path(tempfile.gettempdir())


def socket_path() -> Path:
    """Onde o daemon escuta. Normalmente `$FIREBALL_HOME/daemon.sock`.

    Se esse caminho estourar o limite do AF_UNIX (FIREBALL_HOME aninhado
    fundo — acontece em diretório de teste/scratch), cai para um nome
    derivado por hash no diretório de runtime, que é curto. O caminho
    efetivo aparece em `fireball daemon status`.
    """
    direct = storage.fireball_home() / "daemon.sock"
    if len(str(direct).encode()) <= MAX_SOCKET_PATH:
        return direct
    digest = hashlib.sha256(str(storage.fireball_home()).encode()).hexdigest()[:12]
    return _runtime_dir() / f"fireball-{digest}.sock"


def lock_path() -> Path:
    """Arquivo de exclusão mútua do daemon (flock). Arquivo comum, sem limite
    de tamanho de caminho — por isso não compartilha o fallback do socket."""
    return storage.fireball_home() / "daemon.lock"


def log_path() -> Path:
    return storage.fireball_home() / "daemon.log"


class DaemonError(RuntimeError):
    """Erro que o daemon devolveu — reconstruído no cliente a partir do JSON."""

    def __init__(self, message: str, kind: str = "DaemonError"):
        super().__init__(message)
        self.kind = kind


class DaemonUnavailable(RuntimeError):
    """Não foi possível falar com o daemon (não está de pé, socket morto)."""


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def decode(line: bytes) -> dict:
    return json.loads(line.decode())


def ok(result) -> dict:
    return {"ok": True, "result": result}


def fail(exc: BaseException) -> dict:
    return {"ok": False, "error": {"kind": type(exc).__name__, "message": str(exc)}}
