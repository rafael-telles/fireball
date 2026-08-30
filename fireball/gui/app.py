"""GUI do Fireball: janela (pywebview) + bandeja (QSystemTrayIcon), um único
processo, um único toolkit (Qt) e um único event loop.

A GUI é um viewer + controle fino: toda a lógica de verdade já existe em
`fireball.control`/`fireball.storage`/etc. — os métodos de `Api` só chamam
essas funções, sem duplicar nada da CLI.
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

from fireball import control
from fireball.backends import REALTIME_BACKENDS
from fireball.gui.tray import TrayIcon

WEB_DIR = Path(__file__).resolve().parent / "web"


class Api:
    def get_status(self) -> dict:
        active = control.active_meeting()
        if not active:
            return {"active": None}
        return {"active": control.get_meeting_status(active["id"])}

    def list_backends(self) -> list:
        return list(REALTIME_BACKENDS)

    def start_meeting(self, name: str, fake: bool, backend: str) -> dict:
        return control.start_meeting(name=name, fake=fake, backend=backend)

    def stop_meeting(self, meeting_id: str) -> dict:
        return control.stop_meeting(meeting_id)


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

    tray = TrayIcon(open_window=open_or_focus_window)
    tray.show()

    open_or_focus_window()
    webview.start(gui="qt")


if __name__ == "__main__":
    main()
