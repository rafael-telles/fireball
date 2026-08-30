"""Provedores de resumo plugáveis: cada um recebe a transcrição de uma reunião
e devolve um resumo em markdown.

Mesma forma dos backends de transcrição (`fireball/backends/`), e pelo mesmo
motivo: o resto do Fireball fala com o registro, não com um provedor
específico, então trocar quem escreve o resumo não mexe em nada além daqui.

Adicionar um provedor novo: criar um módulo em fireball/summarizers/,
implementar `SummaryProvider` e registrar em SUMMARY_PROVIDERS abaixo.

Hoje só existe o `claude_code`, que chama o Claude Code instalado na máquina.
Um provedor de API direta (Anthropic, OpenAI) entra sem mudar nada fora deste
pacote — o que muda é a chave e o transporte, não o contrato.
"""

from __future__ import annotations

from typing import Protocol


class SummarizerUnavailable(RuntimeError):
    """Provedor escolhido não está instalado, autenticado ou configurado."""


class SummaryProvider(Protocol):
    """Transforma a transcrição inteira num resumo em markdown.

    Recebe também o `meeting.json` para poder usar nome e data — o provedor
    decide o que fazer com isso. Deve levantar `SummarizerUnavailable` quando
    o problema é de ambiente (binário faltando, sem login) e não da
    transcrição: são erros que o usuário resolve, e a janela mostra a
    diferença.
    """

    name: str

    def summarize(self, transcript: str, meeting: dict) -> str: ...


SUMMARY_PROVIDERS = ("claude_code",)


def get_summary_provider(name: str) -> SummaryProvider:
    if name == "claude_code":
        from fireball.summarizers.claude_code import ClaudeCodeSummarizer

        return ClaudeCodeSummarizer()
    raise SummarizerUnavailable(
        f"Provedor de resumo desconhecido: '{name}'. Use {SUMMARY_PROVIDERS}."
    )
