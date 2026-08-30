"""`fireball-gui` — abre a janela do Fireball.

Desde que a bandeja passou a ser atrelada ao daemon, a janela também mora no
processo dele (ver `fireball.gui.shell`). Então este comando não desenha nada:
garante que o daemon está de pé — o que já faz a bandeja aparecer — e pede a
janela pelo socket, igual a CLI faz com qualquer outra operação.

O efeito prático é o esperado de um app de bandeja: rodar `fireball-gui` duas
vezes não abre duas janelas nem dois ícones, só traz a janela existente pra
frente.
"""

from __future__ import annotations

import sys

from fireball.daemon import client, protocol


def main() -> None:
    try:
        client.ensure_daemon()
        client.call("show_window")
    except protocol.DaemonError as exc:
        # caso típico: daemon subiu sem sessão gráfica, então não há janela
        print(f"fireball-gui: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except protocol.DaemonUnavailable as exc:
        print(f"fireball-gui: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
