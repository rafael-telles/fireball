"""Ícone de bandeja do Fireball via QSystemTrayIcon (PyQt6) — o mesmo
toolkit da janela (pywebview usa Qt aqui), no mesmo processo e no mesmo
event loop.

Não usa GTK/pystray de propósito: misturar GTK (AppIndicator) com Qt em
threads/loops diferentes travou com erros de contexto de thread do
Qt/OpenGL num teste manual. Um toolkit só, um loop só.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from fireball import control

IDLE_COLOR = "#ff6b35"
RECORDING_COLOR = "#33d17a"


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
    def __init__(self, open_window: Callable[[], None]):
        self._idle_icon = _dot_icon(IDLE_COLOR)
        self._recording_icon = _dot_icon(RECORDING_COLOR)
        super().__init__(self._idle_icon)
        self._open_window = open_window
        self.setToolTip("Fireball")

        menu = QMenu()

        open_action = QAction("Abrir Fireball", menu)
        open_action.triggered.connect(lambda: self._open_window())
        menu.addAction(open_action)

        stop_action = QAction("Parar reunião atual", menu)
        stop_action.triggered.connect(self._stop_meeting)
        menu.addAction(stop_action)

        menu.addSeparator()

        quit_action = QAction("Sair", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(2000)
        self._refresh_status()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._open_window()

    def _stop_meeting(self) -> None:
        active = control.active_meeting()
        if active:
            control.stop_meeting(active["id"])

    def _quit(self) -> None:
        # Comportamento de daemon: enquanto o Fireball está "ligado" (ícone
        # na bandeja), pode haver no máximo uma reunião gravando; ao
        # desligar o daemon, essa reunião também para — nada fica orfão.
        active = control.active_meeting()
        if active:
            control.stop_meeting(active["id"])
        QApplication.quit()

    def _refresh_status(self) -> None:
        try:
            active = control.active_meeting()
        except Exception:
            return
        if active:
            self.setIcon(self._recording_icon)
            self.setToolTip(f"Fireball — gravando: {active['name']}")
        else:
            self.setIcon(self._idle_icon)
            self.setToolTip("Fireball")
