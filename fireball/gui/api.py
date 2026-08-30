"""Ponte entre o JavaScript da janela e o núcleo do daemon.

A janela é renderizada **pelo próprio daemon** (ver `fireball.gui.shell`), então
aqui chamamos o `DaemonCore` direto, em processo — não faria sentido o daemon
abrir um socket para falar consigo mesmo. Continua havendo uma autoridade só:
todo método passa pelo mesmo core, sob o mesmo lock que serializa a CLI.

O envelope `{ok, result}` / `{ok, error}` é o mesmo que o socket devolve, para
o front-end (`web/app.js`) não precisar saber por onde a chamada veio.
"""

from __future__ import annotations

from fireball.backends import REALTIME_BACKENDS
from fireball.daemon.core import DaemonCore


class Api:
    def __init__(self, core: DaemonCore):
        self._core = core

    def _call(self, fn, **args) -> dict:
        try:
            return {"ok": True, "result": fn(**args)}
        except Exception as exc:  # noqa: BLE001 — vira mensagem na tela, não crash
            return {"ok": False, "error": str(exc), "kind": type(exc).__name__}

    def get_status(self) -> dict:
        result = self._call(self._core.daemon_status)
        if not result["ok"]:
            return result
        active = result["result"]["active"]
        if not active:
            return {"ok": True, "result": {"active": None}}
        detail = self._call(self._core.meeting_status, meeting_id=active["id"])
        return {"ok": True, "result": {"active": detail["result"]}} if detail["ok"] else detail

    def list_backends(self) -> list:
        return list(REALTIME_BACKENDS)

    def start_meeting(self, name: str, fake: bool, backend: str) -> dict:
        return self._call(self._core.start_meeting, name=name, fake=fake, backend=backend)

    def stop_meeting(self, meeting_id: str) -> dict:
        # sem espera: a janela não pode congelar até o engine fechar os .wav;
        # o status vira 'stopping' e o polling da tela mostra o resto.
        return self._call(self._core.stop_meeting, meeting_id=meeting_id, wait_timeout=0.0)

    def list_meetings(self) -> dict:
        return self._call(self._core.list_meetings)
