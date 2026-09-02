"""Servidor do daemon: socket Unix + instância única + desligamento limpo.

Instância única em duas camadas, de propósito:

1. `flock` exclusivo em `daemon.lock` — é o que de fato garante um daemon só.
   Duas subidas simultâneas (dois clientes tentando auto-subir ao mesmo
   tempo) resolvem aqui, sem janela de corrida: quem perde o flock sai
   quieto e usa o socket de quem ganhou.
2. o socket em si — se sobrou um arquivo de socket de um daemon morto,
   detectamos (ninguém responde ao ping) e removemos antes de fazer bind.
"""

from __future__ import annotations

import fcntl
import os
import signal
import socket
import socketserver
import threading
import traceback
from pathlib import Path
from typing import Optional

from fireball.daemon import protocol
from fireball.daemon.core import DaemonCore


class AlreadyRunning(RuntimeError):
    """Outro daemon já detém o lock."""


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                request = protocol.decode(line)
            except ValueError as exc:
                self._reply({"ok": False, "error": {"kind": "BadRequest", "message": str(exc)}})
                continue
            self._reply(self._dispatch(request))

    def _reply(self, response: dict) -> None:
        try:
            self.wfile.write(protocol.encode(response))
            self.wfile.flush()
        except BrokenPipeError:
            pass  # cliente desistiu no meio; nada a fazer

    def _dispatch(self, request: dict) -> dict:
        core: DaemonCore = self.server.core
        op = request.get("op")
        args = request.get("args") or {}

        handlers = {
            "ping": core.ping,
            "daemon_status": core.daemon_status,
            "active": core.active,
            "start": core.start_meeting,
            "stop": core.stop_meeting,
            "pause": core.pause_meeting,
            "resume": core.resume_meeting,
            "delete": core.delete_meeting,
            "meeting_status": core.meeting_status,
            "transcript_ack": core.transcript_ack,
            "list": core.list_meetings,
            "search": core.search_meetings,
            "note": core.note,
            "rename": core.rename_meeting,
            "set_diarization": core.set_diarization,
            "voices": core.voice_profiles,
            "voice_enroll": core.enroll_voice,
            "voice_rename": core.rename_voice,
            "voice_delete": core.delete_voice,
            "finalize": core.finalize,
            "summarize": core.summarize,
            "prompts": core.prompts,
            "prompt_save": core.save_prompt,
            "prompt_delete": core.delete_prompt,
            "edit_segment": core.edit_segment,
            "delete_segment": core.delete_segment,
            "warnings": core.warnings,
            "agenda": core.agenda,
            "show_window": core.show_window,
            "shutdown": self._shutdown,
        }
        handler = handlers.get(op)
        if handler is None:
            return {"ok": False, "error": {"kind": "UnknownOp", "message": f"Operação desconhecida: {op!r}"}}
        try:
            return protocol.ok(handler(**args))
        except TypeError as exc:
            # argumento errado vindo do cliente — não é falha do daemon
            return {"ok": False, "error": {"kind": "BadRequest", "message": f"{op}: {exc}"}}
        except Exception as exc:  # noqa: BLE001 — o daemon nunca morre por causa de um pedido
            traceback.print_exc()
            return protocol.fail(exc)

    def _shutdown(self) -> dict:
        core: DaemonCore = self.server.core
        result = core.shutdown()  # para a reunião ativa antes de responder
        self.server.stop_event.set()
        return result


class DaemonServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, path: str, core: DaemonCore, stop_event: threading.Event):
        self.core = core
        self.stop_event = stop_event
        super().__init__(path, _Handler)


def daemon_alive(path: Optional[Path] = None, timeout: float = 1.0) -> bool:
    """Tem alguém vivo atendendo neste socket?"""
    path = path or protocol.socket_path()
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(protocol.encode({"op": "ping"}))
            return bool(sock.makefile("rb").readline())
    except OSError:
        return False


def _acquire_lock() -> int:
    lock_file = protocol.lock_path()
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise AlreadyRunning(f"Já existe um daemon do Fireball rodando (lock: {lock_file}).")
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _bind(core: DaemonCore, stop_event: threading.Event) -> DaemonServer:
    path = protocol.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # temos o flock, então ninguém legítimo está atendendo: é resto de um
        # daemon morto.
        path.unlink()
    server = DaemonServer(str(path), core, stop_event)
    os.chmod(path, 0o600)
    return server


def _run_shell(core: DaemonCore, stop_event: threading.Event) -> bool:
    """Roda a bandeja + janela no thread principal, se der. Devolve se rodou.

    A casca é atrelada ao daemon: enquanto o Fireball está ligado, o ícone
    está na bandeja. Quando não há onde desenhar (servidor, ssh, extra [gui]
    não instalado), o daemon segue ligado sem casca em vez de recusar a subir
    — a CLI continua funcionando igual.
    """
    from fireball.gui import shell

    can_draw, reason = shell.available()
    if not can_draw:
        print(f"[daemon] sem bandeja: {reason}", flush=True)
        return False
    shell.run(core, stop_event)
    return True


def run(tray: bool = True) -> int:
    """Sobe o daemon em foreground. Devolve o código de saída."""
    lock_fd = _acquire_lock()
    stop_event = threading.Event()
    core = DaemonCore()

    recovered = core.recover()
    for meeting in recovered:
        print(f"[recover] {meeting['id']} -> {meeting['status']}", flush=True)

    server = _bind(core, stop_event)
    print(f"[daemon] pid={os.getpid()} socket={protocol.socket_path()}", flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_event.set())

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # O loop do Qt precisa ser o do thread principal, então é ele que
        # segura o daemon de pé quando há casca gráfica. Sem casca, o thread
        # principal só espera o sinal de parada.
        if not (tray and _run_shell(core, stop_event)):
            stop_event.wait()
    finally:
        print("[daemon] desligando…", flush=True)
        core.shutdown()
        server.shutdown()
        server.server_close()
        protocol.socket_path().unlink(missing_ok=True)
        os.close(lock_fd)
    return 0
