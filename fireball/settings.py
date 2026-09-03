"""Preferências do Fireball: o que se decide uma vez, não a cada reunião.

Escolher backend na hora de começar uma reunião é pergunta errada no momento
errado — quem vai gravar quer clicar em "iniciar", não decidir motor de
transcrição. Então backend (ao vivo e final) e idioma moram aqui, em
`~/.fireball/settings.json` (ou `$FIREBALL_HOME/settings.json`), e a janela
tem uma tela pra editá-los.

Quem resolve as preferências é o **daemon**, não o cliente: `start`/`finalize`
usam o valor configurado quando o chamador não passa um explicitamente. Isso
mantém a regra de um dono só do estado — e faz a CLI respeitar a mesma
configuração que a janela mostra, sem cada cliente ter sua própria noção de
padrão.

O arquivo é JSON comum de propósito: sem sessão gráfica (servidor, ssh) editar
na mão é o caminho, já que a tela de configuração não existe lá. Ele é escrito
com permissão 600 porque pode guardar chave de API (a do resumo e a da Groq) —
quem prefere não ter segredo em JSON põe a chave no ambiente, que tem
precedência sobre o campo vazio (ver `fireball/summarizers/openai_api.py`).
"""

from __future__ import annotations

import os
from typing import Optional

from fireball import prompts, realtime, storage
from fireball.backends import BATCH_BACKENDS, REALTIME_BACKENDS
from fireball.calendars import CALENDAR_PROVIDERS
from fireball.summarizers import SUMMARY_PROVIDERS

DEFAULTS = {
    # motor da transcrição em tempo real (a que alimenta o chat da janela)
    "realtime_backend": "whisper",
    # transcrever durante a reunião, ou só gravar o áudio e deixar tudo pro final
    "transcribe_live": True,
    # separar pessoas nas duas tracks ao vivo e durante o finalize. Opt-in:
    # Sortformer suporta até quatro vozes na sala e quatro no áudio do sistema.
    "diarize_default": False,
    # rodar a transcrição final. Ela reescreve `transcript.ndjson` por cima —
    # o que é melhor quase sempre, e é perda quando alguém já corrigiu falas à
    # mão, ou quando o backend é pago e ninguém quer ficar a um clique de
    # gastar. Desligada, nem a janela nem `fireball finalize` a rodam.
    "transcribe_final": True,
    # motor da transcrição final, rodada sobre o áudio inteiro depois
    "final_backend": "whisper",
    # chave da Groq, usada só quando final_backend é "groq". Vazio aqui não é
    # erro — significa "não configurado", ou "está no ambiente".
    "groq_api_key": "",
    "language": realtime.DEFAULT_LANGUAGE,
    # quem escreve o resumo da reunião a partir da transcrição
    "summary_provider": "claude_code",
    # qual dos prompts salvos (ver fireball/prompts.py) é usado quando ninguém
    # escolhe um. Reunião já resumida lembra o dela e não passa por aqui.
    "summary_prompt": prompts.BUILTIN_ID,
    # gerar resumo, nome e tags sozinho quando a reunião termina. É o que faz
    # "reunião nasce sem nome, a IA nomeia" acontecer sem ninguém clicar —
    # sem isso a reunião ficaria sem nome até alguém lembrar de pedir.
    "auto_summarize": True,
    # provedor 'openai_api': qualquer API compatível com OpenAI. Vazio aqui
    # não é erro — significa "ainda não configurado", ou "está no ambiente".
    "openai_base_url": "",
    "openai_api_key": "",
    "openai_model": "",
    # quem lê a agenda. Vazio = desligado: instalação limpa não shella nenhum
    # binário até a pessoa escolher um provedor na tela.
    "calendar_provider": "",
    # conta do gog (`--account`). Vazio = deixa o gog resolver sozinho.
    "gog_account": "",
    # qual das vozes cadastradas é a de quem usa o Fireball. É o que junta
    # "Você" (a track do microfone) com o nome que a diarização reconheceu e
    # com o convidado da agenda: sem isso a mesma pessoa aparece três vezes na
    # lista de participantes. Vazio = não configurado, e aí "Você" fica só.
    "my_voice": "",
}

# As chaves cujo valor vazio é uma resposta legítima ("não configurado"), e
# não configuração estragada.
TEXT_KEYS = (
    "openai_base_url",
    "openai_api_key",
    "openai_model",
    "groq_api_key",
    "gog_account",
    "my_voice",
)


def settings_path():
    return storage.fireball_home() / "settings.json"


def _coerce(key: str, value) -> Optional[object]:
    """Devolve o valor validado, ou None se ele não serve para esta chave."""
    if key == "realtime_backend":
        return value if value in REALTIME_BACKENDS else None
    if key == "final_backend":
        return value if value in BATCH_BACKENDS else None
    if key in ("transcribe_live", "transcribe_final", "auto_summarize", "diarize_default"):
        return bool(value)
    if key == "openai_base_url":
        url = str(value or "").strip().rstrip("/")
        if not url:
            return ""
        return url if url.startswith(("http://", "https://")) else None
    if key in TEXT_KEYS:
        return str(value or "").strip()
    if key == "summary_provider":
        return value if value in SUMMARY_PROVIDERS else None
    if key == "summary_prompt":
        # a lista é o registro: id que saiu dela não vale mais como padrão
        return value if value in prompts.ids() else None
    if key == "calendar_provider":
        # vazio é "desligado" de propósito — não cai no padrão de um provedor
        text = str(value or "").strip()
        if not text:
            return ""
        return text if text in CALENDAR_PROVIDERS else None
    if key == "language":
        text = str(value or "").strip()
        return text or None
    return None


def load() -> dict:
    """As preferências efetivas: os padrões com o arquivo por cima.

    Chave desconhecida ou valor inválido (backend que saiu do registro, JSON
    editado errado na mão) é ignorada em silêncio e cai no padrão — uma
    configuração estragada não pode impedir o Fireball de gravar.
    """
    stored = storage.read_json(settings_path(), {}) or {}
    values = dict(DEFAULTS)
    for key, raw in stored.items():
        if key not in DEFAULTS:
            continue
        coerced = _coerce(key, raw)
        if coerced is not None:
            values[key] = coerced
    return values


def save(**fields) -> dict:
    """Aplica os campos recebidos e devolve a configuração inteira já mesclada.

    Aqui, ao contrário do `load()`, valor inválido é erro alto: veio de alguém
    pedindo pra mudar a configuração, e mudar em silêncio para outra coisa
    seria mentir para quem pediu.
    """
    values = load()
    for key, raw in fields.items():
        if key not in DEFAULTS:
            raise ValueError(f"Configuração desconhecida: '{key}'.")
        coerced = _coerce(key, raw)
        if coerced is None:
            if key == "realtime_backend":
                raise ValueError(f"Backend de tempo real inválido: '{raw}'. Use {list(REALTIME_BACKENDS)}.")
            if key == "final_backend":
                raise ValueError(f"Backend de transcrição final inválido: '{raw}'. Use {list(BATCH_BACKENDS)}.")
            if key == "summary_provider":
                raise ValueError(
                    f"Provedor de resumo inválido: '{raw}'. Use {list(SUMMARY_PROVIDERS)}."
                )
            if key == "summary_prompt":
                raise ValueError(
                    f"Prompt de resumo inválido: '{raw}'. Use {prompts.ids()}."
                )
            if key == "calendar_provider":
                raise ValueError(
                    f"Provedor de agenda inválido: '{raw}'. "
                    f"Use '' (desligado) ou {list(CALENDAR_PROVIDERS)}."
                )
            if key == "openai_base_url":
                raise ValueError(
                    f"URL da API inválida: '{raw}'. Ela precisa começar com http:// ou https:// "
                    "(ex.: https://api.openai.com/v1)."
                )
            if key == "language":
                raise ValueError("Idioma não pode ficar vazio (código curto, ex.: pt, en).")
            raise ValueError(f"Valor inválido para '{key}': {raw!r}.")
        values[key] = coerced
    path = settings_path()
    _restrict(path)
    storage.write_json(path, values)
    return values


def _restrict(path) -> None:
    """Deixa o settings.json legível só pelo dono, **antes** de escrever nele.

    Ele pode guardar a chave da API de resumo. Apertar a permissão depois da
    escrita deixaria uma janela — curta, mas real — com o segredo no disco sob
    o umask de todo mundo; por isso o arquivo é criado já com 600 quando ainda
    não existe. Sistema de arquivos sem permissão POSIX não impede configurar:
    o que não dá pra proteger, segue como está.
    """
    try:
        if path.exists():
            os.chmod(path, 0o600)
        else:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError:
        pass
