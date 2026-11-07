"""Quanta memória a janela ocupa — e quando vale recomeçar a página.

A troca para a página ociosa resolveu o daemon **parado**; não resolve a janela
que fica **aberta** por horas. O renderer do QtWebEngine cresce enquanto desenha
e, quando o `VmData` dele encosta no teto de 16 GiB que o próprio Chromium impõe
aos renderers (`RLIMIT_DATA` de 17179869184 bytes, medido em
`/proc/<renderer>/limits`), ele aborta com `Fatal process out of memory:
Worklist::Segment::Create` — e a janela fica **em branco** para sempre, porque o
daemon continua achando que a página do app está carregada.

Medido em 07/11/2026, página em repouso (sem reunião), janela aberta:

- com o anel de `.ok-dot` animando `box-shadow` (era o padrão): **1,04 MB/s** de
  RSS no renderer — os 16 GiB chegam em ~4 h;
- trocando o anel por `transform`/`opacity`: **0,02 MB/s**, o mesmo patamar de
  uma página sem animação nenhuma.

Ou seja, animação que obriga repintura contínua é o que sangra. A CSS já foi
corrigida, e estas duas defesas cobrem o que a CSS não cobre (outro vazamento,
outra versão do QtWebEngine):

1. **vigia de memória**: soma o RSS+swap dos processos do QtWebEngine e pede a
   troca de página ao passar de `LIMITE_MB`;
2. **renderer morto**: `renderProcessTerminated` recarrega na hora.

A regra é pura e o `/proc` entra por parâmetro, então dá para testar sem Qt e
sem processo nenhum.
"""

from __future__ import annotations

import os
from typing import Callable, Iterable, Optional

# Acima disso a página é recomeçada. Uma janela em uso normal (reunião aberta,
# transcrição na tela) fica na casa das centenas de MB; 1,2 GB é folga de sobra
# e ainda muito longe dos 16 GiB que derrubam o renderer.
LIMITE_MB = 1200

# Durante uma gravação o recomeço custa mais caro: a tela volta para o Início e
# o que estava sendo lido se perde no meio da reunião. Aí o vigia só age perto
# do teto em que o renderer morreria de qualquer jeito.
LIMITE_GRAVANDO_MB = 4000

# o executável dos processos que o QtWebEngine cria (renderer, zygote, utility)
PROCESSO_WEBENGINE = "QtWebEngineProcess"


def _status(pid: int) -> str:
    with open(f"/proc/{pid}/status", encoding="utf-8") as arquivo:
        return arquivo.read()


def _pais() -> dict[int, int]:
    """`{pid: ppid}` de tudo que existe agora. Serve para achar os descendentes."""
    pais: dict[int, int] = {}
    for nome in os.listdir("/proc"):
        if not nome.isdigit():
            continue
        try:
            with open(f"/proc/{nome}/stat", "rb") as arquivo:
                bruto = arquivo.read()
            # o nome do processo vem entre parênteses e pode ter espaço: o ppid
            # é o segundo campo depois do último ')'
            campos = bruto[bruto.rindex(b")") + 2:].split()
            pais[int(nome)] = int(campos[1])
        except (OSError, ValueError, IndexError):
            continue  # processo que morreu no meio da leitura
    return pais


def descendentes(raiz: int, pais: Optional[dict[int, int]] = None) -> list[int]:
    """Todos os descendentes de `raiz` (filhos, netos, ...)."""
    tabela = _pais() if pais is None else pais
    filhos: dict[int, list[int]] = {}
    for pid, ppid in tabela.items():
        filhos.setdefault(ppid, []).append(pid)
    saida: list[int] = []
    pilha = [raiz]
    while pilha:
        for filho in filhos.get(pilha.pop(), ()):
            saida.append(filho)
            pilha.append(filho)
    return saida


def memoria_mb(pid: int, ler: Optional[Callable[[int], str]] = None) -> float:
    """RSS + swap de um processo, em MB. Processo que já foi conta zero."""
    ler = ler if ler is not None else _status
    try:
        texto = ler(pid)
    except OSError:
        return 0.0
    total = 0
    for linha in texto.splitlines():
        if linha.startswith(("VmRSS:", "VmSwap:")):
            total += int(linha.split()[1])
    return total / 1024


def _e_webengine(pid: int, ler_link: Optional[Callable[[int], str]] = None) -> bool:
    ler_link = ler_link if ler_link is not None else (lambda p: os.readlink(f"/proc/{p}/exe"))
    try:
        return ler_link(pid).endswith(PROCESSO_WEBENGINE)
    except OSError:
        return False


def memoria_dos_renderers(
    raiz: Optional[int] = None,
    *,
    pais: Optional[dict[int, int]] = None,
    ler: Optional[Callable[[int], str]] = None,
    e_webengine: Optional[Callable[[int], bool]] = None,
) -> float:
    """Quanto os processos do QtWebEngine filhos de `raiz` ocupam, em MB.

    Só eles entram na conta: o daemon carrega os modelos de fala (centenas de
    MB) e misturar isso com a página faria o vigia recomeçar a janela à toa.
    """
    raiz = os.getpid() if raiz is None else raiz
    filtro = e_webengine if e_webengine is not None else _e_webengine
    pids: Iterable[int] = descendentes(raiz, pais)
    return sum(memoria_mb(pid, ler) for pid in pids if filtro(pid))


def deve_reciclar(mb: float, gravando: bool = False) -> bool:
    """A página passou do teto? Com reunião gravando o teto é bem mais alto."""
    return mb >= (LIMITE_GRAVANDO_MB if gravando else LIMITE_MB)
