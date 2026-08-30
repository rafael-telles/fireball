"""GUI do Fireball: janela (pywebview) + bandeja (QSystemTrayIcon), um único
processo, um único toolkit (Qt) e um único event loop.

A GUI é um **cliente do daemon**: ela não lê nem escreve arquivo de reunião
nenhum, não sobe processo de gravação e não guarda estado próprio — todo
método de `Api` é uma chamada ao daemon (`fireball.daemon.client`), igual ao
que a CLI faz. É por isso que abrir a janela no meio de uma reunião iniciada
pela CLI mostra o estado certo, e vice-versa: existe uma fonte de verdade só.

O daemon sobe sozinho quando a GUI abre (`ensure_daemon`), e continua sendo
um processo separado, sem Qt — assim ele roda igual em máquina sem sessão
gráfica, e quem só usa a CLI não paga PyQt6.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Precisa ser importado antes de qualquer QApplication ser criada (exigência
# do Qt/WebEngine) — senão o pywebview falha ao carregar o backend Qt mais
# tarde com "QtWebEngineWidgets must be imported ... before a
# QCoreApplication instance is created" (erro real visto em teste manual).
from PyQt6 import QtWebEngineWidgets  # noqa: F401

import webview
from PyQt6.QtWidgets import QApplication

from fireball.backends import REALTIME_BACKENDS
from fireball.daemon import client, protocol
from fireball.gui.tray import TrayIcon

WEB_DIR = Path(__file__).resolve().parent / "web"


class Api:
    """Ponte JS → daemon. Cada método é uma chamada ao daemon; erros do
    daemon viram `{"error": ...}` para o front-end mostrar, em vez de
    estourarem dentro do pywebview."""

    def _call(self, op: str, **args):
        try:
            return {"ok": True, "result": client.call(op, **args)}
        except (protocol.DaemonError, protocol.DaemonUnavailable) as exc:
            return {"ok": False, "error": str(exc), "kind": getattr(exc, "kind", type(exc).__name__)}

    def get_status(self) -> dict:
        result = self._call("daemon_status")
        if not result["ok"]:
            return result
        active = result["result"]["active"]
        if not active:
            return {"ok": True, "result": {"active": None}}
        detail = self._call("meeting_status", meeting_id=active["id"])
        return {"ok": True, "result": {"active": detail["result"]}} if detail["ok"] else detail

    def list_backends(self) -> list:
        return list(REALTIME_BACKENDS)

    def start_meeting(self, name: str, fake: bool, backend: str) -> dict:
        return self._call("start", name=name, fake=fake, backend=backend)

    def stop_meeting(self, meeting_id: str) -> dict:
        # sem espera: a janela não pode congelar até o engine fechar os .wav;
        # o status vira 'stopping' e o polling da tela mostra o resto.
        return self._call("stop", meeting_id=meeting_id, wait_timeout=0.0)

    def list_meetings(self) -> dict:
        return self._call("list")


def _on_closing(window) -> bool:
    """Fechar a janela (X, Alt+F4) só esconde — o app continua na bandeja.

    `QApplication.setQuitOnLastWindowClosed(False)` NÃO resolve isso: o
    próprio pywebview chama `_app.exit()` explicitamente dentro do
    closeEvent quando a última instância de BrowserView fecha
    (webview/platforms/qt.py, `if len(BrowserView.instances) == 0:
    self.hide(); _app.exit()`), ignorando esse ajuste do Qt por completo —
    confirmado lendo o código-fonte da lib depois de um bug real onde a
    bandeja sumia junto com a janela. Cancelar o close aqui (retornando
    False) impede que esse trecho seja alcançado.
    """
    window.hide()
    return False  # False cancela o close de verdade (ver webview/event.py)


def open_or_focus_window() -> None:
    """Reaproveita a janela se ela já existir (só escondida); evita duplicar
    se o usuário clicar "Abrir Fireball" mais de uma vez."""
    if webview.windows:
        webview.windows[0].show()
        return

    window = webview.create_window(
        "Fireball",
        url=str(WEB_DIR / "index.html"),
        js_api=Api(),
        width=460,
        height=440,
        resizable=False,
        background_color="#0b0b0f",
    )
    window.events.closing += _on_closing


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)

    # sobe o daemon antes de desenhar qualquer coisa; se ele não subir, a
    # bandeja ainda aparece (em estado "offline") e tenta reconectar sozinha.
    try:
        client.ensure_daemon()
    except protocol.DaemonUnavailable as exc:
        print(f"[gui] daemon não subiu: {exc}", file=sys.stderr)

    tray = TrayIcon(open_window=open_or_focus_window)
    tray.show()

    open_or_focus_window()
    webview.start(gui="qt")


if __name__ == "__main__":
    main()
