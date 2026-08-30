"""Ícone de bandeja do Fireball — a presença visível do daemon.

Roda dentro do processo do daemon (ver `fireball.gui.shell`), então consulta o
`DaemonCore` direto, em memória. Não há estado de "daemon fora do ar" aqui de
propósito: o ícone existe exatamente enquanto o daemon existe, que é o ponto
de atrelar um ao outro.

QSystemTrayIcon (PyQt6) e não GTK/pystray: misturar o AppIndicator do pystray
(GTK/GLib) com o Qt do pywebview em loops diferentes travou com erros de
contexto de thread do Qt/OpenGL num teste manual. Um toolkit só.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from fireball.daemon.core import DaemonCore

STATE_COLORS = {
    "idle": "#ff6b35",
    "recording": "#33d17a",
    "stopping": "#e5a50a",
}

STATE_LABEL = {
    "starting": "iniciando",
    "recording": "gravando",
    "stopping": "parando",
}


def _dot_icon(color: str) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(QColor(color))
    painter.drawEllipse(8, 8, 48, 48)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, core: DaemonCore, show_window: Callable[[], None], quit_app: Callable[[], None]):
        self._icons = {state: _dot_icon(color) for state, color in STATE_COLORS.items()}
        super().__init__(self._icons["idle"])
        self._core = core
        self._show_window = show_window
        self._quit_app = quit_app
        self._active: Optional[dict] = None
        self.setToolTip("Fireball")

        menu = QMenu()

        open_action = QAction("Abrir Fireball", menu)
        open_action.triggered.connect(lambda: self._show_window())
        menu.addAction(open_action)

        self._stop_action = QAction("Parar reunião atual", menu)
        self._stop_action.triggered.connect(self._stop_meeting)
        self._stop_action.setEnabled(False)
        menu.addAction(self._stop_action)

        menu.addSeparator()

        quit_action = QAction("Sair (desliga o Fireball)", menu)
        quit_action.triggered.connect(lambda: self._quit_app())
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(2000)
        self._refresh_status()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_window()

    def _stop_meeting(self) -> None:
        if not self._active:
            return
        try:
            # sem espera: o engine ainda fecha os .wav depois do sinal e a UI
            # não pode travar por isso — o próximo tick mostra 'parando'.
            self._core.stop_meeting(self._active["id"], wait_timeout=0.0)
        except Exception as exc:  # noqa: BLE001
            self.showMessage("Fireball", f"Não consegui parar a reunião: {exc}")

    def _refresh_status(self) -> None:
        try:
            active = self._core.active()
        except Exception:  # noqa: BLE001 — um tick que falha não pode matar a bandeja
            return

        self._active = active
        if not active:
            self.setIcon(self._icons["idle"])
            self.setToolTip("Fireball — ocioso")
            self._stop_action.setEnabled(False)
            return

        status = active.get("status", "recording")
        self.setIcon(self._icons["stopping" if status == "stopping" else "recording"])
        self.setToolTip(f"Fireball — {STATE_LABEL.get(status, status)}: {active['name']}")
        self._stop_action.setEnabled(status != "stopping")
