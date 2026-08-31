"""Provedores de resumo plugáveis: cada um recebe a transcrição de uma reunião
e devolve o nome dela, as tags e o resumo em markdown.

Mesma forma dos backends de transcrição (`fireball/backends/`), e pelo mesmo
motivo: o resto do Fireball fala com o registro, não com um provedor
específico, então trocar quem escreve o resumo não mexe em nada além daqui.

Adicionar um provedor novo: criar um módulo em fireball/summarizers/,
implementar `SummaryProvider` e registrar em SUMMARY_PROVIDERS abaixo. O
prompt e a leitura da resposta são compartilhados (`fireball/summarizers/
prompt.py`) — o que cada provedor implementa é o transporte, não o pedido. As
`instructions` são o miolo configurável desse pedido (ver `fireball.prompts`):
o provedor só as repassa a `build_prompt`, sem interpretá-las.

Os dois de hoje:

- `claude_code` — chama o Claude Code instalado na máquina. Não pede chave nem
  configuração; o custo é do plano de quem roda.
- `openai_api` — qualquer API compatível com OpenAI (a da OpenAI, OpenRouter,
  Groq, Ollama, llama.cpp, vLLM, LM Studio…): você diz a URL, a chave e o
  modelo, e o provedor não precisa saber quem está do outro lado.
"""

from __future__ import annotations

from typing import Protocol


class SummarizerUnavailable(RuntimeError):
    """Provedor escolhido não está instalado, autenticado ou configurado."""


class SummarizerFailed(RuntimeError):
    """O provedor respondeu, mas o que voltou não serve como resumo.

    Separado de `SummarizerUnavailable` porque as duas pedem coisas diferentes
    de quem lê o erro: uma é configuração para arrumar, a outra é tentar de
    novo (ou trocar de modelo).
    """


class SummaryProvider(Protocol):
    """Lê a transcrição inteira e devolve `{markdown, title, tags}`.

    As três coisas saem de uma leitura só — ver o porquê em
    `fireball.summarizers.prompt`. `title` pode voltar None (o provedor não
    conseguiu nomear), e `tags` pode voltar vazia; o resumo é o que não pode
    faltar.

    Recebe também o `meeting.json` para poder usar nome e data — o provedor
    decide o que fazer com isso. Deve levantar `SummarizerUnavailable` quando
    o problema é de ambiente (binário faltando, sem login, sem URL
    configurada) e não da transcrição: são erros que o usuário resolve, e a
    janela mostra a diferença.
    """

    name: str

    def summarize(self, transcript: str, meeting: dict, instructions: str = "") -> dict: ...


SUMMARY_PROVIDERS = ("claude_code", "openai_api")


def get_summary_provider(name: str) -> SummaryProvider:
    if name == "claude_code":
        from fireball.summarizers.claude_code import ClaudeCodeSummarizer

        return ClaudeCodeSummarizer()
    if name == "openai_api":
        from fireball.summarizers.openai_api import OpenAICompatibleSummarizer

        return OpenAICompatibleSummarizer()
    raise SummarizerUnavailable(
        f"Provedor de resumo desconhecido: '{name}'. Use {SUMMARY_PROVIDERS}."
    )
