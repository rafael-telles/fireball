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
na mão é o caminho, já que a tela de configuração não existe lá.
"""

from __future__ import annotations

from typing import Optional

from fireball import realtime, storage
from fireball.backends import BATCH_BACKENDS, REALTIME_BACKENDS
from fireball.summarizers import SUMMARY_PROVIDERS

DEFAULTS = {
    # motor da transcrição em tempo real (a que alimenta o chat da janela)
    "realtime_backend": "whisper",
    # transcrever durante a reunião, ou só gravar o áudio e deixar tudo pro final
    "transcribe_live": True,
    # motor da transcrição final, rodada sobre o áudio inteiro depois
    "final_backend": "whisper",
    "language": realtime.DEFAULT_LANGUAGE,
    # quem escreve o resumo da reunião a partir da transcrição
    "summary_provider": "claude_code",
}


def settings_path():
    return storage.fireball_home() / "settings.json"


def _coerce(key: str, value) -> Optional[object]:
    """Devolve o valor validado, ou None se ele não serve para esta chave."""
    if key == "realtime_backend":
        return value if value in REALTIME_BACKENDS else None
    if key == "final_backend":
        return value if value in BATCH_BACKENDS else None
    if key == "transcribe_live":
        return bool(value)
    if key == "summary_provider":
        return value if value in SUMMARY_PROVIDERS else None
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
            if key == "language":
                raise ValueError("Idioma não pode ficar vazio (código curto, ex.: pt, en).")
            raise ValueError(f"Valor inválido para '{key}': {raw!r}.")
        values[key] = coerced
    storage.write_json(settings_path(), values)
    return values
