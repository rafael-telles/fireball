"""A casca gráfica do daemon: bandeja + janela, rodando *dentro* do processo
do daemon.

A bandeja é atrelada ao daemon de propósito: **se o Fireball está ligado, ele
aparece na bandeja**. Não existe daemon rodando sem ícone nem ícone sem
daemon — o ícone é a resposta visual à pergunta "isso está ligado?". Por isso
o daemon não é mais um processo sem interface: quando há sessão gráfica, o
loop principal dele *é* o loop do Qt.

Sem sessão gráfica (servidor, ssh) ou sem o extra `[gui]` instalado, o daemon
continua subindo normalmente, só que sem casca — ver `available()` e o
caminho headless em `fireball.daemon.server.run`.

Um toolkit só, um processo, um event loop: a janela é pywebview com backend
Qt, a bandeja é QSystemTrayIcon (parte do PyQt6, sem lib extra) e as duas
dividem o mesmo `QApplication`. Misturar GTK (AppIndicator do pystray) com o
Qt do pywebview travou com erros de contexto de thread do Qt/OpenGL num teste
manual.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from fireball.daemon.core import DaemonCore

WEB_DIR = Path(__file__).resolve().parent / "web"

# de quanto em quanto tempo o loop do Qt checa se pediram pra desligar. Também
# é o que dá chance de os handlers de sinal do Python rodarem: enquanto o Qt
# está parado dentro do exec() em C++, o interpretador não processa SIGTERM.
STOP_POLL_MS = 200


def available() -> tuple[bool, str]:
    """Dá pra desenhar a casca aqui? Devolve (pode, motivo_se_nao)."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "sem sessão gráfica (DISPLAY/WAYLAND_DISPLAY não definidos)"
    try:
        import PyQt6  # noqa: F401
        import webview  # noqa: F401
    except ImportError as exc:
        return False, f"extra [gui] não instalado ({exc})"
    return True, ""


def run(core: DaemonCore, stop_event: threading.Event) -> None:
    """Roda a casca no thread atual (que precisa ser o principal — exigência
    do Qt). Só retorna quando `stop_event` for disparado ou o usuário sair
    pela bandeja."""
    # Precisa ser importado antes de qualquer QApplication ser criada
    # (exigência do Qt/WebEngine) — senão o pywebview falha ao carregar o
    # backend Qt mais tarde com "QtWebEngineWidgets must be imported ...
    # before a QCoreApplication instance is created" (erro real visto em
    # teste manual).
    from PyQt6 import QtWebEngineWidgets  # noqa: F401

    import webview
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal
    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

    from fireball.gui.api import Api
    from fireball.gui.tray import TrayIcon

    class Bridge(QObject):
        """Traz para o thread do Qt os pedidos que chegam pelo socket.

        `show_window` pode ser chamado por qualquer thread do servidor, e
        mexer em widget fora do thread do Qt é falha na certa. Emitir um
        signal cruza a fronteira com segurança (conexão enfileirada)."""

        show_requested = pyqtSignal()

    app = QApplication.instance() or QApplication(sys.argv)
    # a janela some pra bandeja em vez de fechar; sem isso o Qt encerraria o
    # daemon junto com ela
    app.setQuitOnLastWindowClosed(False)

    window = webview.create_window(
        "Fireball",
        url=str(WEB_DIR / "index.html"),
        js_api=Api(core),
        width=820,
        height=640,
        min_size=(560, 460),
        resizable=True,  # o chat da transcrição ganha (ou perde) tela junto
        background_color="#0b0b0f",
        hidden=True,  # o daemon sobe mostrando só a bandeja
    )

    def on_closing() -> bool:
        """Fechar a janela (X, Alt+F4) só esconde — o daemon continua ligado,
        e continua na bandeja.

        `setQuitOnLastWindowClosed(False)` sozinho NÃO resolve: o próprio
        pywebview chama `_app.exit()` explicitamente dentro do closeEvent
        quando a última instância de BrowserView fecha
        (webview/platforms/qt.py, `if len(BrowserView.instances) == 0:
        self.hide(); _app.exit()`), ignorando esse ajuste do Qt por completo —
        confirmado lendo o código-fonte da lib depois de um bug real onde a
        bandeja sumia junto com a janela. Cancelar o close aqui (retornando
        False) impede que esse trecho seja alcançado.
        """
        window.hide()
        return False  # False cancela o close de verdade (ver webview/event.py)

    window.events.closing += on_closing

    bridge = Bridge()
    bridge.show_requested.connect(window.show)
    # quem chegar pelo socket pedindo a janela (`fireball-gui`) cai aqui
    core.set_window_opener(bridge.show_requested.emit)

    def request_quit() -> None:
        """Sair pela bandeja: desliga o daemon inteiro.

        Em thread separada porque parar a reunião ativa espera o engine fechar
        os .wav — travar a UI nesse meio-tempo faria o app parecer pendurado.
        Quem realmente encerra o loop é o timer abaixo, ao ver o stop_event.
        """
        threading.Thread(target=lambda: (core.shutdown(), stop_event.set()), daemon=True).start()

    tray = TrayIcon(core=core, show_window=bridge.show_requested.emit, quit_app=request_quit)
    tray.show()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(
            "[shell] atenção: este ambiente não expõe bandeja do sistema; "
            "o Fireball está rodando, mas sem ícone.",
            file=sys.stderr,
            flush=True,
        )

    quit_timer = QTimer()
    quit_timer.timeout.connect(lambda: app.quit() if stop_event.is_set() else None)
    quit_timer.start(STOP_POLL_MS)

    # Ao desligar, o Qt imprime "Release of profile requested but
    # WebEnginePage still not deleted. Expect troubles!" — é a ordem de
    # destruição interna do QtWebEngine com o pywebview, no fim do processo.
    # Tentei destruir a janela antes de encerrar o loop (na mesma volta e na
    # seguinte) e o aviso continua; o desligamento é limpo de todo jeito
    # (reunião parada, .wav fechados, socket removido, nada vazando), então
    # fica registrado como ruído conhecido em vez de código que não resolve.
    try:
        webview.start(gui="qt")
    finally:
        core.set_window_opener(None)
        tray.hide()
        stop_event.set()  # janela fechada de vez == daemon desligando
