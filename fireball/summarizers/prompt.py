"""O pedido que todo provedor de resumo faz, e como ler a resposta.

Mora fora dos provedores porque **o pedido é o mesmo** — muda o transporte
(um binário na máquina, uma API HTTP), não o trabalho. Deixar cada provedor
com o seu prompt faria as reuniões saírem resumidas de um jeito diferente
conforme quem estava configurado no dia, o que é exatamente o que a lista de
provedores plugáveis não deveria custar.

São três coisas num pedido só (nome, tags, resumo) de propósito: as três saem
da mesma leitura da transcrição, e pedi-las separadamente seria pagar a
transcrição inteira três vezes — em plano, em token, ou em minuto de modelo
local.

Por isso a resposta vem em JSON, e não em markdown puro como antes. O preço é
ter de aguentar modelo que não obedece formato: `parse_result` aceita cerca de
código em volta, texto antes e depois, e cai para "isso aqui é o resumo
inteiro" quando não achar JSON nenhum — perder o nome e as tags é bem melhor
que perder o resumo.

O pedido é montado em três partes, e **só a do meio é configurável** (ver
`fireball.prompts`): `HEAD` explica de onde vem a transcrição e fecha o
contrato JSON, `TAIL` põe as guardas que valem para qualquer resumo, e no meio
entram as instruções de o que o resumo deve dizer. Deixar o contrato fora do
alcance de quem escreve prompt é o que garante que **todo** prompt continue
devolvendo nome e tags — um prompt livre que esquecesse de pedir o JSON faria
a reunião parar de ganhar nome sozinha, e o sintoma apareceria longe da causa.
"""

from __future__ import annotations

import json
import re

from fireball.summarizers import SummarizerFailed

# Tags demais deixam de agrupar coisa nenhuma: se toda reunião tem oito tags,
# nenhuma delas separa uma reunião das outras.
MAX_TAGS = 5
MAX_TAG_LEN = 40
MAX_TITLE_LEN = 80

HEAD = """\
Você está lendo a transcrição de uma reunião gravada pelo Fireball. Seu
trabalho é dar um nome à reunião, classificá-la com tags e escrever o resumo.

A transcrição vem uma fala por linha, no formato `Falante: texto`. Ela é feita
por transcrição automática: espere erros de palavra, pontuação estranha e
frases cortadas. Interprete pelo contexto e não cite trechos que não fazem
sentido.

Os rótulos de falante significam o seguinte:
- Sem diarização, "Você" é toda a track do microfone e "Outros participantes"
  é toda a track do áudio do sistema. Cada uma pode conter mais de uma pessoa.
- Com diarização, "Sala N" separa vozes captadas pelo microfone daqui e
  "Remoto N" separa vozes vindas do áudio do sistema.
- Os números são identidades anônimas desta gravação, não nomes. Não invente
  nomes nem associe convidados da agenda a uma voz sem evidência explícita.
- Quando aparece o nome de uma pessoa no lugar de "Sala N"/"Remoto N", ele
  veio de um perfil de voz cadastrado e pode ser usado no resumo.

Responda **só com um objeto JSON**, sem cerca de código e sem nenhum texto
antes ou depois, com exatamente estas três chaves:

{
  "title": "nome curto da reunião, até 60 caracteres, sem data nem hora",
  "tags": ["3 a 5 tags", "minúsculas", "uma ou duas palavras cada"],
  "summary": "o resumo em markdown, no formato abaixo"
}\
"""

# A parte configurável: o que o resumo deve dizer, e em que forma. Este é o
# texto que a tela de configuração edita, e o que cada prompt salvo substitui.
DEFAULT_INSTRUCTIONS = """\
O markdown de "summary" segue esta estrutura:

<um parágrafo, no máximo três frases, dizendo o que a reunião resolveu>

## Decisões
- <o que ficou decidido; omita a seção inteira se nada foi decidido>

## Em aberto
- <o que ficou sem resposta; omita a seção inteira se não houver>\
"""

TAIL = """\
Escreva tudo em português do Brasil. Não abra o resumo com o título nem o
repita — ele já vai em "title". As tags dizem assunto e tipo da reunião
("produto", "contratação", "1:1", "planejamento"): elas servem para agrupar
reuniões parecidas, então não use nome de pessoa nem tag que só valeria para
esta reunião.

Não invente nada que não esteja na transcrição. Se ela for curta ou não tiver
conteúdo real, diga isso em uma frase no "summary" em vez de preencher as
seções, e dê um título honesto (por exemplo "Conversa curta sem assunto
definido").\
"""


def build_prompt(meeting: dict, instructions: str = "") -> str:
    """O pedido inteiro: as partes fixas com as instruções escolhidas no meio.

    Instruções vazias caem no padrão — um prompt salvo sem texto nenhum não
    pode virar um pedido sem forma nenhuma.

    O nome da reunião só entra quando existe: reunião sem nome é o padrão
    agora, e mandar "o nome desta reunião é: Sem nome" ancoraria o modelo
    justamente no que ele foi chamado para escrever.

    O evento de agenda (quando a reunião nasceu a partir de um) entra na
    parte fixa — título, descrição e convidados já estavam escritos **antes**
    da conversa, e são o melhor contexto do que a reunião deveria ter sido.
    Não vai no miolo configurável: todo provedor precisa disso, e um prompt
    livre que esquecesse de pedir o contexto perderia justamente o ganho.
    """
    middle = (instructions or "").strip() or DEFAULT_INSTRUCTIONS.strip()
    parts = [f"{HEAD.strip()}\n\n{middle}\n\n{TAIL.strip()}"]

    name = (meeting.get("name") or "").strip()
    if name:
        if meeting.get("name_source") == "calendar":
            parts.append(
                f"Esta reunião já tem um nome vindo da agenda: {name}. "
                "Não invente outro título — use este (ou uma forma bem próxima) em \"title\"."
            )
        else:
            parts.append(f"Esta reunião já tem um nome dado por quem gravou: {name}")

    event_block = _event_context(meeting.get("event"))
    if event_block:
        parts.append(event_block)
    return "\n\n".join(parts)


MAX_EVENT_DESCRIPTION = 1500


def _event_context(event) -> str:
    """Bloco fixo com o que a agenda sabia da reunião antes dela começar."""
    if not isinstance(event, dict):
        return ""
    title = (event.get("title") or "").strip()
    description = (event.get("description") or "").strip()
    if len(description) > MAX_EVENT_DESCRIPTION:
        description = description[:MAX_EVENT_DESCRIPTION].rstrip() + "…"
    location = (event.get("location") or "").strip()
    people = []
    for person in event.get("attendees") or []:
        if not isinstance(person, dict):
            continue
        label = (person.get("name") or person.get("email") or "").strip()
        if label:
            people.append(label)
    if not (title or description or location or people):
        return ""

    lines = [
        "A reunião foi iniciada a partir deste evento de agenda. Use como "
        "contexto do que a conversa *deveria* ter sido — não invente nada "
        "que não esteja na transcrição, e não trate a pauta como se tivesse "
        "acontecido se a conversa foi para outro lado:",
    ]
    if title:
        lines.append(f"- Título: {title}")
    if location:
        lines.append(f"- Local: {location}")
    if people:
        lines.append(f"- Convidados: {', '.join(people)}")
    if description:
        lines.append(f"- Descrição / pauta:\n{description}")
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """Tira a cerca de código que o modelo põe em volta do JSON mesmo depois de
    lhe pedirem que não ponha."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]  # ```json
    while lines and not lines[-1].strip().startswith("```"):
        lines.pop()
    if lines:
        lines.pop()
    return "\n".join(lines).strip()


def _escape_raw_newlines(text: str) -> str:
    """Escapa quebra de linha crua dentro de string JSON.

    É *a* forma de o JSON vir quebrado aqui, e por um motivo específico: o campo
    "summary" é markdown de várias linhas, e o modelo que escreve JSON à mão
    (sem saída estruturada do servidor — o caso do `claude_code`) tende a
    quebrar a linha de verdade em vez de escrever `\\n`. O resto do objeto está
    perfeito; recusá-lo por isso jogaria fora o resumo inteiro.

    Só toca no que está entre aspas, respeitando a barra invertida — fora das
    strings a quebra de linha é espaço em branco legítimo e não muda nada.
    """
    out = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string and not escaped and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        if escaped:
            escaped = False
        elif ch == "\\" and in_string:
            escaped = True
        elif ch == '"':
            in_string = not in_string
        out.append(ch)
    return "".join(out)


def _load_json(text: str):
    """O objeto JSON de dentro da resposta, ou None se não houver um.

    Tenta o texto inteiro e, se falhar, o maior trecho entre a primeira `{` e a
    última `}` — que é o que sobra quando o modelo escreve "Claro! Aqui está:"
    antes do objeto. Cada um deles também é tentado com as quebras de linha
    cruas escapadas (ver `_escape_raw_newlines`).
    """
    slice_ = text[text.find("{") : text.rfind("}") + 1]
    for candidate in (text, slice_, _escape_raw_newlines(text), _escape_raw_newlines(slice_)):
        if not candidate.strip():
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean_title(raw) -> str | None:
    title = re.sub(r"\s+", " ", str(raw or "")).strip().strip("#").strip()
    # modelo que devolve o título entre aspas é comum o bastante pra tratar
    title = title.strip("\"'“”").strip()
    if not title:
        return None
    return title[:MAX_TITLE_LEN].strip()


def _clean_tags(raw) -> list[str]:
    """Tags como a tela vai mostrá-las: minúsculas, sem `#`, sem repetição.

    Normalizar aqui, e não na exibição, é o que faz "Produto" e "produto"
    virarem a mesma tag entre duas reuniões — que é a única coisa que uma tag
    precisa fazer.
    """
    if isinstance(raw, str):
        raw = re.split(r"[,;]", raw)
    if not isinstance(raw, list):
        return []

    tags: list[str] = []
    seen = set()
    for item in raw:
        tag = re.sub(r"\s+", " ", str(item or "")).strip().lstrip("#").strip().lower()
        tag = tag[:MAX_TAG_LEN].strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) == MAX_TAGS:
            break
    return tags


def _as_markdown(raw) -> str:
    """O campo "summary" como texto, mesmo quando ele não veio como texto.

    Um prompt que peça bullets faz o modelo devolver `summary` como lista JSON
    em vez de string — e aí `str()` produziria o repr da lista dentro do
    resumo. Como o prompt é configurável, esse formato deixou de ser desvio
    exótico: vira markdown de lista, que é o que a pessoa pediu.
    """
    if isinstance(raw, list):
        return "\n".join(f"- {str(item).strip()}" for item in raw if str(item).strip())
    return str(raw or "").strip()


def parse_result(raw: str) -> dict:
    """A resposta do provedor virada em `{markdown, title, tags}`.

    Sem JSON legível, tudo que veio é tratado como o resumo: um provedor que
    resolveu responder em markdown ainda entregou a parte que a pessoa vai ler,
    e recusar a resposta inteira por causa do formato perderia o trabalho todo.
    """
    text = (raw or "").strip()
    if not text:
        raise SummarizerFailed("O provedor devolveu uma resposta vazia.")

    data = _load_json(_strip_fences(text))
    if data is None:
        return {"markdown": text, "title": None, "tags": []}

    markdown = _as_markdown(data.get("summary"))
    if not markdown:
        raise SummarizerFailed(
            "O provedor devolveu um JSON sem o campo 'summary' — não veio resumo nenhum."
        )
    return {
        "markdown": markdown,
        "title": _clean_title(data.get("title")),
        "tags": _clean_tags(data.get("tags")),
    }
