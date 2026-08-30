"""Cliente do daemon — usado pela CLI e pela GUI, que não têm outra forma de
mexer em estado.

Uma conexão por chamada, de propósito: a frequência é baixa (a bandeja
consulta a cada 2s) e assim não há socket meio-morto pra gerenciar em GUI e
CLI. O custo de um connect em socket Unix local é irrelevante nessa escala.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from fireball.daemon import protocol

CONNECT_TIMEOUT = 2.0
# subir o daemon importa o pacote e reconcilia o que sobrou; 15s é folga larga
SPAWN_TIMEOUT = 15.0


def _connect(timeout: float) -> socket.socket:
    path = protocol.socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError as exc:
        sock.close()
        raise protocol.DaemonUnavailable(f"Daemon do Fireball não está atendendo em {path}: {exc}") from exc
    return sock


def is_running() -> bool:
    try:
        _connect(CONNECT_TIMEOUT).close()
        return True
    except protocol.DaemonUnavailable:
        return False


def spawn_daemon() -> subprocess.Popen:
    """Sobe o daemon destacado, com log em `$FIREBALL_HOME/daemon.log`."""
    log_file = open(protocol.log_path(), "a")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "fireball.daemon"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()


def ensure_daemon(timeout: float = SPAWN_TIMEOUT) -> None:
    """Garante que há um daemon atendendo, subindo um se preciso.

    Se dois clientes fizerem isso ao mesmo tempo, um perde o flock e sai
    quieto — os dois acabam usando o mesmo daemon (ver `server._acquire_lock`).
    """
    if is_running():
        return
    spawn_daemon()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.1)
    raise protocol.DaemonUnavailable(
        f"O daemon não subiu em {timeout:.0f}s. Veja {protocol.log_path()} ou rode `fireball daemon run`."
    )


def call(op: str, timeout: Optional[float] = 30.0, autostart: bool = True, **args):
    """Manda uma operação ao daemon e devolve o resultado (ou levanta o erro
    que ele reportou, com o tipo preservado)."""
    if autostart:
        ensure_daemon()
    sock = _connect(CONNECT_TIMEOUT)
    try:
        sock.settimeout(timeout)
        sock.sendall(protocol.encode({"op": op, "args": args}))
        line = sock.makefile("rb").readline()
    except OSError as exc:
        raise protocol.DaemonUnavailable(f"Falha falando com o daemon: {exc}") from exc
    finally:
        sock.close()

    if not line:
        raise protocol.DaemonUnavailable("O daemon fechou a conexão sem responder.")
    response = protocol.decode(line)
    if response.get("ok"):
        return response["result"]
    error = response.get("error") or {}
    raise protocol.DaemonError(error.get("message", "erro desconhecido"), error.get("kind", "DaemonError"))


def shutdown(timeout: float = 120.0) -> Optional[dict]:
    """Desliga o daemon (e, com ele, a reunião ativa). Não sobe um daemon só
    pra desligá-lo em seguida."""
    if not is_running():
        return None
    return call("shutdown", timeout=timeout, autostart=False)
