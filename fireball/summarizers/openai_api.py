"""Resumo por qualquer API compatível com OpenAI: você diz a URL, a chave e o
modelo.

"Compatível com OpenAI" virou o protocolo comum de fato — a própria OpenAI,
OpenRouter, Groq, Together, Ollama, llama.cpp, vLLM, LM Studio e a maioria dos
servidores locais falam `POST /v1/chat/completions` com o mesmo corpo. Então o
provedor não é "o provedor da OpenAI": é um provedor sem dono, e quem escolhe
de quem é o modelo é a configuração.

Duas decisões que a implementação carrega:

- **`urllib` da biblioteca padrão**, e não o SDK da OpenAI. É uma chamada HTTP
  com um JSON de ida e um de volta; um SDK aqui seria uma dependência a mais
  para instalar (e uma a mais para quebrar) sem trazer nada — e ele amarraria o
  Fireball ao dialeto de um fornecedor justamente no lugar cujo objetivo é não
  ter fornecedor.
- **`response_format: json_object` com repique**. Pedimos JSON estruturado
  porque é o que faz nome, tags e resumo virem numa chamada só; mas nem todo
  servidor compatível aceita esse campo, e alguns recusam a requisição inteira
  por causa dele. Então uma recusa 4xx faz o pedido ser repetido sem o campo —
  o prompt já pede JSON por escrito, e `prompt.parse_result` aguenta a resposta
  vir suja.

A chave sai da configuração (`~/.fireball/settings.json`, escrito com
permissão 600) ou do ambiente, nessa ordem. O ambiente existe para quem prefere
manter segredo em `.env` e fora do JSON — e é o caminho natural num daemon sem
sessão gráfica.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fireball.summarizers import SummarizerFailed, SummarizerUnavailable
from fireball.summarizers.prompt import build_prompt, parse_result

# Um resumo de reunião longa em modelo local pode demorar de verdade; mais que
# isso é sinal de que algo travou do outro lado.
TIMEOUT_S = 600

# Determinismo importa mais que variedade aqui: o mesmo áudio deve dar o mesmo
# nome e as mesmas tags se alguém regerar.
TEMPERATURE = 0.2

# Serviço atrás de Cloudflare bloqueia o User-Agent padrão do urllib
# (403 "error code: 1010") antes mesmo de olhar a chave — daí a nossa.
USER_AGENT = "fireball"

# Gateway que roteia entre modelos (o zen do opencode é um) recusa a requisição
# inteira sem um id estável de conversa no cabeçalho. Mandamos o id da reunião:
# regerar o resumo da mesma reunião cai na mesma conversa, que é o que deixa o
# cache de prompt do outro lado servir para alguma coisa.
SESSION_HEADER = "x-opencode-session"

ENV_BASE_URL = "FIREBALL_OPENAI_BASE_URL"
ENV_API_KEY = "FIREBALL_OPENAI_API_KEY"
ENV_MODEL = "FIREBALL_OPENAI_MODEL"


def _setting(configured: str, env_var: str) -> str:
    """A configuração, com o ambiente como reserva.

    A configuração vem primeiro porque foi alguém digitando na tela; o ambiente
    é o caminho de quem guarda segredo em `.env` e não quer a chave dentro do
    settings.json.
    """
    value = (configured or "").strip()
    return value or os.environ.get(env_var, "").strip()


def chat_completions_url(base_url: str) -> str:
    """A URL do endpoint a partir do que a pessoa digitou.

    Aceita as três formas que aparecem nos README por aí — `https://host`,
    `https://host/v1` e a URL completa até `/chat/completions` — porque errar
    isso rende um 404 sem explicação, e adivinhar aqui é barato.
    """
    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


class OpenAICompatibleSummarizer:
    name = "openai_api"

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "") -> None:
        # sem argumentos, sai da configuração — é assim que o registro
        # (`get_summary_provider`) constrói o provedor
        from fireball import settings

        prefs = settings.load()
        self.base_url = _setting(base_url or prefs["openai_base_url"], ENV_BASE_URL)
        self.api_key = _setting(api_key or prefs["openai_api_key"], ENV_API_KEY)
        self.model = _setting(model or prefs["openai_model"], ENV_MODEL)

    # ------------------------------------------------------------- http

    def _post(self, payload: dict, session: str = "") -> dict:
        headers = {
            "Content-Type": "application/json",
            # servidor local costuma ignorar a chave; mandar mesmo assim é
            # inofensivo e evita um caminho de código a menos pra testar
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
        }
        if session:
            headers[SESSION_HEADER] = session
        request = urllib.request.Request(
            chat_completions_url(self.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise _http_error(exc.code, detail, self.base_url) from exc
        except urllib.error.URLError as exc:
            raise SummarizerUnavailable(
                f"Não consegui falar com {self.base_url}: {exc.reason}. "
                "Confira a URL da API e se o servidor está no ar."
            ) from exc
        except TimeoutError as exc:
            raise SummarizerUnavailable(
                f"A API em {self.base_url} não respondeu em {TIMEOUT_S}s."
            ) from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SummarizerFailed(
                f"Resposta inesperada da API (não era JSON): {body[:300]!r}"
            ) from exc

    # -------------------------------------------------------------- api

    def summarize(self, transcript: str, meeting: dict, instructions: str = "") -> dict:
        if not self.base_url:
            raise SummarizerUnavailable(
                "Falta a URL da API compatível com OpenAI. Preencha em Configurações → "
                f"Resumo, ou defina {ENV_BASE_URL} no ambiente."
            )
        if not self.model:
            raise SummarizerUnavailable(
                "Falta o id do modelo da API compatível com OpenAI. Preencha em "
                f"Configurações → Resumo, ou defina {ENV_MODEL} no ambiente."
            )

        payload = {
            "model": self.model,
            "temperature": TEMPERATURE,
            "messages": [
                {"role": "system", "content": build_prompt(meeting, instructions)},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
        }
        session = str(meeting.get("id") or "")
        try:
            data = self._post(payload, session)
        except SummarizerUnavailable as exc:
            # servidor compatível que não conhece response_format recusa a
            # requisição inteira; o prompt já pede JSON, então vai sem o campo
            if not _rejected_response_format(exc):
                raise
            payload.pop("response_format")
            data = self._post(payload, session)

        return parse_result(_content(data))


def _http_error(code: int, detail: str, base_url: str) -> Exception:
    """Traduz o status HTTP na frase que diz o que fazer.

    401/403 e 404 são erro de configuração (chave errada, URL errada, modelo
    que não existe naquele servidor) e viram `SummarizerUnavailable`, que é a
    classe que a janela mostra como "arrume isto". 5xx é o outro lado com
    problema — tentar de novo é a resposta certa, não mexer na configuração.

    Um 403 cujo corpo não é o JSON de erro da API não é sobre a chave: é um
    CDN na frente do provedor recusando o cliente, e a mensagem diz isso.

    O status vai junto no `.status` da exceção: é por ele que a chamada decide
    repetir sem `response_format`, e não pelo texto da mensagem, que muda de
    servidor para servidor.
    """
    api_message = _api_message(detail)
    message = api_message or detail[:300] or f"HTTP {code}"
    if code == 403 and api_message is None:
        # 403 sem erro de API no corpo não veio do provedor, e sim de um proxy/CDN
        # na frente dele barrando o cliente (Cloudflare: "error code: 1010").
        # Mandar conferir a chave aqui é mandar procurar no lugar errado.
        error = SummarizerUnavailable(
            f"{chat_completions_url(base_url)} barrou a requisição antes de chegar na API "
            f"(403): {message}. Não é a chave — é o serviço na frente dela recusando este cliente."
        )
    elif code in (401, 403):
        error = SummarizerUnavailable(
            f"A API recusou a chave ({code}): {message}. Confira a chave em Configurações → Resumo."
        )
    elif code == 404:
        error = SummarizerUnavailable(
            f"A API respondeu 404 em {chat_completions_url(base_url)}: {message}. "
            "Confira a URL (costuma terminar em /v1) e o id do modelo."
        )
    elif code == 429:
        error = SummarizerFailed(f"A API está limitando as chamadas (429): {message}")
    elif code >= 500:
        error = SummarizerFailed(f"A API falhou ({code}): {message}")
    else:
        error = SummarizerUnavailable(f"A API recusou a requisição ({code}): {message}")
    error.status = code
    return error


def _error_message(body) -> str | None:
    """A mensagem de dentro do `error` do corpo, quando há uma."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "").strip() or None
    if isinstance(error, str):
        return error.strip() or None
    return None


def _api_message(detail: str) -> str | None:
    """O mesmo, para um corpo de erro que ainda é texto cru."""
    try:
        return _error_message(json.loads(detail))
    except (json.JSONDecodeError, TypeError):
        return None


def _rejected_response_format(exc: Exception) -> bool:
    """Vale repetir sem `response_format`?

    Um pedido malformado (4xx que não é de credencial) num servidor compatível
    é quase sempre este campo — ele é a única coisa fora do mínimo comum que
    mandamos. Repetir uma vez custa uma requisição e salva todo servidor que
    não implementa saída estruturada.
    """
    status = getattr(exc, "status", None)
    return status in (400, 415, 422) or "response_format" in str(exc)


def _content(data: dict) -> str:
    """O texto da resposta, com o erro certo quando ele não está lá.

    Alguns servidores devolvem 200 com um `error` no corpo em vez do status
    HTTP; tratar isso como "resposta vazia" mandaria a pessoa caçar log atrás
    de uma mensagem que já veio pronta.
    """
    message = _error_message(data)
    if message:
        raise SummarizerFailed(f"A API devolveu um erro: {message}")

    choices = data.get("choices") or []
    if not choices:
        raise SummarizerFailed(f"A API não devolveu nenhuma resposta: {json.dumps(data)[:300]}")
    content = (choices[0].get("message") or {}).get("content")
    if not (content or "").strip():
        raise SummarizerFailed("A API devolveu uma resposta vazia.")
    return content
