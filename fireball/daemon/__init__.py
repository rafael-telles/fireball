"""Daemon do Fireball: o processo dono do estado.

CLI (`fireball.cli`) e GUI (`fireball.gui`) são clientes puros — nenhum dos
dois escreve em `meeting.json` nem sobe processo de gravação. Os dois falam
com o daemon por socket Unix (ver `protocol.py`), e o sobem sozinhos se ele
não estiver de pé (ver `client.ensure_daemon`).

    protocol.py  formato das mensagens, caminhos do socket/lock
    core.py      estado + supervisão dos processos filhos (engine, finalize)
    server.py    socket Unix, instância única, desligamento limpo
    client.py    cliente fino usado por CLI e GUI
"""
