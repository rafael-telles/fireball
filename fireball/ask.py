"""Perguntar sobre uma reunião já transcrita.

Não é o resumo. O resumo lê a reunião inteira uma vez e devolve um documento;
aqui a mesma matéria serve de contexto para uma pergunta curta, com a conversa
anterior junto — o que muda a forma do pedido, não o que é preciso saber.

Duas decisões que valem registro:

- **Só o provedor `openai_api` atende.** O `claude_code` é um binário que
  escreve arquivo em disco, sem sessão de conversa: enfiar uma pergunta nele
  daria uma resposta isolada, sem lembrar da anterior, e o chat viraria um
  botão que responde a primeira pergunta e erra a segunda. Quando ele for o
  provedor escolhido, a janela diz isso em vez de fingir.
- **O contexto é montado aqui, não no provedor.** A transcrição é a fonte; as
  notas são o que a pessoa achou importante; o resumo já é uma leitura. Mandar
  os três, com o trecho da transcrição por falante e horário, é o que permite
  a resposta citar "quem falou" — e é o que o Granola faz ao abrir a origem.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Iterable, Optional

from fireball import control, settings
from fireball.summarizers import SummarizerFailed, SummarizerUnavailable

# Reunião de seis horas não cabe (nem deve) num contexto de pergunta. O corte é
# por caracteres porque é o que o modelo cobra; a cauda é o que interessa menos
# numa pergunta sobre a conversa toda, então cortamos o começo.
MAX_CONTEXT_CHARS = 24000
MAX_HISTORY_TURNS = 12

SYSTEM = """Você responde perguntas sobre uma reunião que já foi transcrita.

Regras:
- Responda em português, curto: duas ou três frases quando bastar.
- Use só o que está na transcrição, nas notas e no resumo. Se a resposta não
  estiver lá, diga que a reunião não cobre isso — não invente.
- Quando a resposta vier de uma fala, diga quem falou e o horário, e termine o
  trecho com o número da fala entre colchetes (`[12]`) — a janela transforma
  isso numa lupa que abre a transcrição.
- Não repita o contexto de volta; vá direto à resposta."""


class AskUnavailable(RuntimeError):
    """Não dá para perguntar: provedor escolhido não atende chat, ou falta configuração."""


def _hm(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}" if total >= 3600 else f"{total // 60:02d}:{total % 60:02d}"


def build_context(
    segments: Iterable[dict],
    notes: str = "",
    summary_md: str = "",
    limit: int = MAX_CONTEXT_CHARS,
) -> str:
    """A matéria da resposta: falas, notas e resumo, nessa ordem de confiança."""
    linhas = []
    for segment in segments:
        quem = (segment.get("speaker") or segment.get("speaker_key") or "").strip()
        texto = (segment.get("text") or "").strip()
        if not texto:
            continue
        marca = _hm(segment.get("start") or 0)
        linhas.append(f"[{marca}] {quem}: {texto}" if quem else f"[{marca}] {texto}")

    corpo = "\n".join(linhas)
    if len(corpo) > limit:
        corpo = "…\n" + corpo[-limit:]

    partes = ["TRANSCRIÇÃO:", corpo or "(sem falas)"]
    if notes.strip():
        partes += ["", "NOTAS DE QUEM GRAVOU:", notes.strip()[:4000]]
    if summary_md.strip():
        partes += ["", "RESUMO JÁ GERADO:", summary_md.strip()[:6000]]
    return "\n".join(partes)


def build_messages(
    question: str,
    history: Optional[list[dict]],
    context: str,
) -> list[dict]:
    """system + contexto + histórico + a pergunta de agora."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "system", "content": context},
    ]
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        papel = "assistant" if turn.get("role") == "assistant" else "user"
        texto = (turn.get("content") or "").strip()
        if texto:
            messages.append({"role": papel, "content": texto})
    messages.append({"role": "user", "content": question.strip()})
    return messages


def _provider_for_ask(prefs: dict):
    nome = prefs.get("summary_provider") or "openai_api"
    if nome != "openai_api":
        raise AskUnavailable(
            "Perguntar sobre a reunião precisa de um provedor de API "
            "(Configurações → Resumo → Provedor). O Claude Code não mantém "
            "conversa, então ele escreve o resumo mas não responde perguntas."
        )
    from fireball.summarizers.openai_api import OpenAICompatibleSummarizer

    return OpenAICompatibleSummarizer()


def answer(
    meeting_id: str,
    question: str,
    history: Optional[list[dict]] = None,
    prefs: Optional[dict] = None,
) -> dict:
    """Responde a pergunta e devolve `{answer, model}`."""
    pergunta = (question or "").strip()
    if not pergunta:
        raise AskUnavailable("Pergunta vazia.")

    prefs = prefs if prefs is not None else settings.load()
    control.read_meeting(meeting_id)
    segments = control.read_transcript(meeting_id)
    context = build_context(segments, _notes_of(meeting_id), _latest_summary(meeting_id))
    texto, model = _ask_provider(
        prefs, build_messages(pergunta, history, context), f"fireball-ask-{meeting_id}"
    )
    return {"answer": texto, "model": model}


def _notes_of(meeting_id: str) -> str:
    try:
        return control.read_notes(meeting_id) or ""
    except Exception:  # nota é opcional; sem ela a resposta continua possível
        return ""


def _latest_summary(meeting_id: str) -> str:
    try:
        from fireball import summaries, storage

        itens = summaries.listing(storage.meeting_path(meeting_id))
        if not itens:
            return ""
        resumo = summaries.get(storage.meeting_path(meeting_id), itens[-1]["id"])
        return (resumo or {}).get("markdown") or ""
    except Exception:
        return ""


def _ask_provider(prefs: dict, messages: list[dict], session: str) -> tuple[str, str]:
    provider = _provider_for_ask(prefs)
    if not getattr(provider, "base_url", ""):
        raise AskUnavailable(
            "Falta a URL da API em Configurações → Resumo. O chat usa o mesmo "
            "provedor do resumo."
        )
    try:
        texto = provider.chat(messages, session=session)
    except SummarizerUnavailable as exc:
        raise AskUnavailable(str(exc)) from exc
    except SummarizerFailed as exc:
        raise AskUnavailable(f"A resposta não veio utilizável: {exc}") from exc
    return (texto or "").strip(), getattr(provider, "model", "")


# ------------------------------------------------- todas as reuniões

# O acervo inteiro não cabe num pedido, então a pergunta escolhe as reuniões:
# a busca do catálogo (nome, tags, participantes, notas, resumo e falas) acha as
# que falam do assunto, e as mais recentes completam — "o que ficou pendente
# essa semana?" não tem palavra-chave nenhuma para buscar.
MAX_LIBRARY_MEETINGS = 8
MAX_LIBRARY_CHARS = 36000
MAX_SUMMARY_PER_MEETING = 3000
MAX_NOTES_PER_MEETING = 800

SYSTEM_LIBRARY = """Você responde perguntas sobre o histórico de reuniões de quem usa o Fireball.

Regras:
- Responda em português, curto: duas ou três frases quando bastar; lista quando
  a resposta juntar várias reuniões.
- Use só o que está nas reuniões abaixo. Se a resposta não estiver lá, diga que
  as reuniões encontradas não cobrem isso — não invente.
- Cada reunião tem um rótulo como [R3]. Logo depois de cada informação, cite o
  rótulo da reunião de onde ela veio ([R3]); a janela transforma isso num link
  que abre a reunião.
- Quando a pergunta envolver tempo ("semana passada", "ontem"), use as datas
  das reuniões e diga a data na resposta.
- Não repita o contexto de volta; vá direto à resposta."""

_STOPWORDS = {
    "que", "qual", "quais", "quem", "quando", "onde", "como", "porque", "sobre",
    "para", "pela", "pelo", "pelas", "pelos", "com", "sem", "uma", "umas", "uns",
    "das", "dos", "nas", "nos", "num", "numa", "isso", "isto", "esse", "essa",
    "este", "esta", "aquele", "aquela", "foi", "foram", "ser", "seria", "tem",
    "tinha", "teve", "houve", "fizemos", "falamos", "falou", "disse", "ficou",
    "ficaram", "reunião", "reuniões", "reuniao", "reunioes", "semana", "mês",
    "ontem", "hoje", "último", "última", "ultimo", "ultima", "passada", "passado",
    "mais", "menos", "muito", "algum", "alguma", "alguém", "todos", "todas",
    "minha", "minhas", "meu", "meus", "nossa", "nosso", "gente", "você", "vocês",
    "decidimos", "decidido", "decidiu", "combinamos", "combinado",
}
_WORD = re.compile(r"[\wÀ-ÿ]{3,}", re.UNICODE)


def question_terms(question: str, limit: int = 8) -> list[str]:
    """As palavras da pergunta que valem uma busca: sem as que toda pergunta tem."""
    vistos: list[str] = []
    for palavra in _WORD.findall(question or ""):
        termo = palavra.lower()
        if termo in _STOPWORDS or termo.isdigit() or termo in vistos:
            continue
        vistos.append(termo)
    return vistos[:limit]


def pick_meetings(
    question: str,
    search: Callable[..., dict],
    recent: Callable[[], list[dict]],
    limit: int = MAX_LIBRARY_MEETINGS,
    pinned: Iterable[str] = (),
) -> list[dict]:
    """As reuniões que entram no contexto, da mais nova para a mais antiga.

    Cada termo é buscado sozinho: a busca do catálogo exige todos os termos ao
    mesmo tempo, e uma pergunta em linguagem natural quase nunca tem todas as
    palavras na mesma reunião. Quem casa com mais termos entra primeiro.
    `pinned` são as reuniões da resposta anterior: "e na mais recente?" fala
    delas, e sem fixá-las a busca da continuação traria outras.
    """
    pontos: dict[str, int] = {}
    achadas: dict[str, dict] = {}
    for termo in question_terms(question):
        for meeting in (search(termo, limit=20) or {}).get("meetings", []):
            mid = meeting["id"]
            pontos[mid] = pontos.get(mid, 0) + 1
            atual = achadas.setdefault(mid, {**meeting, "hits": []})
            for hit in meeting.get("hits") or []:
                if len(atual["hits"]) < 4 and hit not in atual["hits"]:
                    atual["hits"].append(hit)

    recentes = recent()
    por_id = {m["id"]: m for m in recentes}
    fixadas = [
        achadas.get(mid) or {**por_id[mid], "hits": []}
        for mid in dict.fromkeys(pinned)
        if mid in achadas or mid in por_id
    ]
    ordem = sorted(
        achadas.values(),
        key=lambda m: (-pontos[m["id"]], _neg_time(m.get("started_at"))),
    )
    escolhidas = fixadas[:limit]
    ids = {m["id"] for m in escolhidas}
    for meeting in [*ordem, *recentes]:
        if len(escolhidas) >= limit:
            break
        if meeting["id"] not in ids and meeting.get("segments", 1):
            escolhidas.append({"hits": [], **meeting})
            ids.add(meeting["id"])
    return sorted(escolhidas, key=lambda m: _neg_time(m.get("started_at")))


def _neg_time(started_at) -> float:
    try:
        return -datetime.fromisoformat(started_at).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _when(started_at) -> str:
    try:
        return datetime.fromisoformat(started_at).astimezone().strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "data desconhecida"


def build_library_context(
    meetings: list[dict],
    summary_of: Callable[[str], str],
    notes_of: Callable[[str], str],
    limit: int = MAX_LIBRARY_CHARS,
) -> tuple[str, list[dict]]:
    """O texto de contexto e a tabela rótulo → reunião que a janela usa nos links."""
    blocos: list[str] = []
    fontes: list[dict] = []
    usado = 0
    for indice, meeting in enumerate(meetings, start=1):
        rotulo = f"R{indice}"
        nome = meeting.get("name") or "Sem nome"
        cabeca = f"=== [{rotulo}] {nome} · {_when(meeting.get('started_at'))}"
        tags = meeting.get("tags") or []
        if tags:
            cabeca += f" · tags: {', '.join(tags)}"
        partes = [cabeca]

        resumo = (summary_of(meeting["id"]) or "").strip()
        if resumo:
            partes += ["RESUMO:", resumo[:MAX_SUMMARY_PER_MEETING]]
        notas = (notes_of(meeting["id"]) or "").strip()
        if notas:
            partes += ["NOTAS DE QUEM GRAVOU:", notas[:MAX_NOTES_PER_MEETING]]
        trechos = [
            f"- {h.get('speaker') or '?'}: {h.get('snippet')}"
            for h in meeting.get("hits") or []
            if (h.get("snippet") or "").strip(" …")
        ]
        if trechos:
            partes += ["FALAS QUE CASAM COM A PERGUNTA:", *trechos]
        if len(partes) == 1:
            partes.append("(sem resumo nem notas)")

        bloco = "\n".join(partes)
        if blocos and usado + len(bloco) > limit:
            break
        blocos.append(bloco)
        usado += len(bloco)
        fontes.append(
            {"label": rotulo, "id": meeting["id"], "name": nome, "started_at": meeting.get("started_at")}
        )

    if not blocos:
        return "Nenhuma reunião gravada ainda.", []
    return "\n\n".join(blocos), fontes


def answer_library(
    question: str,
    history: Optional[list[dict]] = None,
    prefs: Optional[dict] = None,
) -> dict:
    """Responde sobre todas as reuniões e devolve `{answer, model, sources}`."""
    pergunta = (question or "").strip()
    if not pergunta:
        raise AskUnavailable("Pergunta vazia.")

    from fireball import catalog

    prefs = prefs if prefs is not None else settings.load()
    # o contexto sai da pergunta junto com a anterior: "e com o Guilherme?"
    # sozinha não acharia a reunião de que a conversa está falando
    anteriores = " ".join(
        turn.get("content") or "" for turn in (history or [])[-2:] if turn.get("role") == "user"
    )
    fixadas = next(
        (turn.get("sources") or [] for turn in reversed(history or []) if turn.get("role") == "assistant"),
        [],
    )
    escolhidas = pick_meetings(
        f"{anteriores} {pergunta}", catalog.search, catalog.meeting_summaries, pinned=fixadas
    )
    context, fontes = build_library_context(escolhidas, _latest_summary, _notes_of)
    messages = build_messages(pergunta, history, context)
    messages[0] = {"role": "system", "content": SYSTEM_LIBRARY}
    texto, model = _ask_provider(prefs, messages, "fireball-ask-library")
    return {"answer": texto, "model": model, "sources": fontes}
