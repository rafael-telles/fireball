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

from typing import Iterable, Optional

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
    meeting = control.read_meeting(meeting_id)
    segments = control.read_transcript(meeting_id)
    notes = ""
    try:
        notes = control.read_notes(meeting_id) or ""
    except Exception:  # nota é opcional; sem ela a resposta continua possível
        notes = ""
    summary_md = ""
    try:
        from fireball import summaries, storage

        itens = summaries.listing(storage.meeting_path(meeting_id))
        if itens:
            resumo = summaries.get(storage.meeting_path(meeting_id), itens[-1]["id"])
            summary_md = (resumo or {}).get("markdown") or ""
    except Exception:
        summary_md = ""

    context = build_context(segments, notes, summary_md)
    provider = _provider_for_ask(prefs)
    if not getattr(provider, "base_url", ""):
        raise AskUnavailable(
            "Falta a URL da API em Configurações → Resumo. O chat usa o mesmo "
            "provedor do resumo."
        )
    messages = build_messages(pergunta, history, context)

    try:
        texto = provider.chat(messages, session=f"fireball-ask-{meeting_id}")
    except SummarizerUnavailable as exc:
        raise AskUnavailable(str(exc)) from exc
    except SummarizerFailed as exc:
        raise AskUnavailable(f"A resposta não veio utilizável: {exc}") from exc

    return {"answer": (texto or "").strip(), "model": getattr(provider, "model", "")}
