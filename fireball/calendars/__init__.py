"""Provedores de agenda plugáveis: cada um devolve os próximos eventos num
formato comum ao Fireball.

Mesma forma dos backends de transcrição e dos summarizers, e pelo mesmo
motivo: o resto do Fireball fala com o registro, não com um provedor
específico. Trocar quem lê o calendário não mexe em nada além daqui.

Adicionar um provedor novo: criar um módulo em fireball/calendars/,
implementar `CalendarProvider` e registrar em CALENDAR_PROVIDERS abaixo.

O de hoje:

- `gog` — chama o `gog` (gogcli) instalado na máquina, já autenticado. Não
  pede client_id, client_secret nem refresh token; a credencial fica no
  keyring do gogcli. Nomeado pelo transporte, como `claude_code`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class CalendarUnavailable(RuntimeError):
    """Provedor escolhido não está instalado, autenticado ou configurado."""


class CalendarFailed(RuntimeError):
    """O provedor respondeu, mas o que voltou não serve.

    Separado de `CalendarUnavailable` porque as duas pedem coisas diferentes
    de quem lê o erro: uma é configuração para arrumar, a outra é tentar de
    novo.
    """


class CalendarProvider(Protocol):
    """Lista eventos numa janela de tempo, já normalizados.

    Cada item é um dict Fireball (ver `fireball.calendars.gog` para o
    contrato). Deve levantar `CalendarUnavailable` quando o problema é de
    ambiente (binário faltando, sem login, conta não resolvida) e
    `CalendarFailed` quando a resposta veio estranha.
    """

    name: str

    def upcoming(self, since: datetime, until: datetime, limit: int = 8) -> list[dict]: ...


CALENDAR_PROVIDERS = ("gog",)


def get_calendar_provider(name: str) -> CalendarProvider:
    if name == "gog":
        from fireball.calendars.gog import GogCalendar

        return GogCalendar()
    raise CalendarUnavailable(
        f"Provedor de agenda desconhecido: '{name}'. Use {CALENDAR_PROVIDERS}."
    )
