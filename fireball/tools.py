"""Onde os binários externos moram quando o PATH do processo é curto.

O Fireball delega credencial a CLIs que a pessoa já autenticou — `gog` para
a agenda, `claude` para o resumo, `nemo-speech` para diarização. Achar esses
binários parecia trabalho do `shutil.which` e pronto, até o daemon subir de
um clique no lançador do desktop.

Lançador não é shell de login: não passa pelo ~/.zshrc, não roda
`brew shellenv`. O PATH que chega ao processo é o mínimo da sessão gráfica,
e um `gog` instalado pelo Homebrew deixa de existir para o `which`. O
sintoma é o pior tipo: a janela diz que não conseguiu ler a agenda enquanto
o binário está instalado, autenticado e respondendo no terminal.

A regra aqui: PATH primeiro — quem exportou exportou de propósito, e uma
versão escolhida à mão continua ganhando — e depois os diretórios onde os
gerenciadores de pacote de usuário instalam por padrão. A busca devolve um
caminho; ninguém mexe em `os.environ`, que é estado do processo inteiro e
vazaria para todo subprocess que o daemon abrir.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# Instaladores de usuário mais comuns, na ordem em que confiamos neles.
# `~/.local/bin` (pip, pipx, npm global) e Homebrew — Linux, macOS Intel e
# Apple Silicon — cobrem quase todos os casos; os gerenciadores por
# linguagem vêm depois porque colidem mais entre si.
FALLBACK_DIRS = (
    "~/.local/bin",
    "~/bin",
    "/usr/local/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "~/.linuxbrew/bin",
    "/opt/homebrew/bin",
    "~/.cargo/bin",
    "~/.bun/bin",
    "~/go/bin",
)

# Instalação fora do previsto: a saída de emergência sem editar código. Vale
# como PATH (separado por ':') e é consultada antes dos padrões.
PATH_ENV = "FIREBALL_PATH"


def find_binary(name: str) -> Optional[str]:
    """Caminho do executável, ou None. É o `shutil.which` com plano B."""
    found = shutil.which(name)
    if found:
        return found

    for directory in fallback_dirs():
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def fallback_dirs() -> list[Path]:
    """Os diretórios do plano B, já expandidos e sem repetição."""
    entries = [
        part for part in os.environ.get(PATH_ENV, "").split(os.pathsep) if part.strip()
    ]
    entries += FALLBACK_DIRS

    dirs: list[Path] = []
    for entry in entries:
        path = Path(entry).expanduser()
        if path not in dirs:
            dirs.append(path)
    return dirs
