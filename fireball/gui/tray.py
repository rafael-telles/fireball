"""Ícone de bandeja do Fireball via QSystemTrayIcon (PyQt6) — o mesmo
toolkit da janela (pywebview usa Qt aqui), no mesmo processo e no mesmo
event loop.

Não usa GTK/pystray de propósito: misturar GTK (AppIndicator) com Qt em
threads/loops diferentes travou com erros de contexto de thread do
Qt/OpenGL num teste manual. Um toolkit só, um loop só.

A bandeja é a *cara* do daemon, não o daemon: ela só pergunta o estado a ele
(uma chamada em memória, não uma varredura de disco como antes) e mostra o
resultado em três estados — gravando, ocioso, daemon fora do ar.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from fireball.daemon import client, protocol

IDLE_COLOR = "#ff6b35"
RECORDING_COLOR = "#33d17a"
OFFLINE_COLOR = "#6b6b76"


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
        self._icons = {
            "idle": _dot_icon(IDLE_COLOR),
            "recording": _dot_icon(RECORDING_COLOR),
            "offline": _dot_icon(OFFLINE_COLOR),
        }
        super().__init__(self._icons["offline"])
        self._open_window = open_window
        self._active: Optional[dict] = None
        self.setToolTip("Fireball")

        menu = QMenu()

        open_action = QAction("Abrir Fireball", menu)
        open_action.triggered.connect(lambda: self._open_window())
        menu.addAction(open_action)

        self._stop_action = QAction("Parar reunião atual", menu)
        self._stop_action.triggered.connect(self._stop_meeting)
        self._stop_action.setEnabled(False)
        menu.addAction(self._stop_action)

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
        if not self._active:
            return
        try:
            # sem espera: o engine ainda fecha os .wav depois do sinal e a UI
            # não pode travar por isso — o próximo tick mostra 'stopping'.
            client.call("stop", meeting_id=self._active["id"], wait_timeout=0.0)
        except (protocol.DaemonError, protocol.DaemonUnavailable) as exc:
            self.showMessage("Fireball", f"Não consegui parar a reunião: {exc}")

    def _quit(self) -> None:
        # A bandeja é a presença visível do daemon: sair daqui desliga o
        # daemon, que por sua vez para a reunião ativa e fecha os arquivos
        # direito. Nada continua gravando sem ninguém olhando.
        try:
            client.shutdown()
        except (protocol.DaemonError, protocol.DaemonUnavailable):
            pass
        QApplication.quit()

    def _refresh_status(self) -> None:
        try:
            # autostart desligado: o polling da bandeja não deve ressuscitar
            # um daemon que o usuário desligou de propósito.
            status = client.call("daemon_status", autostart=False, timeout=3.0)
        except (protocol.DaemonError, protocol.DaemonUnavailable):
            self._apply("offline", "Fireball — daemon fora do ar", None)
            return

        active = status.get("active")
        if active:
            self._apply("recording", f"Fireball — {active['status']}: {active['name']}", active)
        else:
            self._apply("idle", "Fireball — ocioso", None)

    def _apply(self, state: str, tooltip: str, active: Optional[dict]) -> None:
        self._active = active
        self.setIcon(self._icons[state])
        self.setToolTip(tooltip)
        self._stop_action.setEnabled(active is not None)
