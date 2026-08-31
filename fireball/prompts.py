"""Os prompts de resumo salvos: uma lista que a tela de configuração administra.

Um "prompt", aqui, **não** é o pedido inteiro que vai para o modelo — é só o
miolo dele, as instruções de o que o resumo deve dizer e em que forma. O
contrato (de onde vem a transcrição, o que significam os dois falantes, e o
JSON com `title`/`tags`/`summary`) fica fora do alcance de quem escreve
prompt, em `fireball.summarizers.prompt`. Por isso qualquer prompt salvo
continua rendendo nome e tags: nenhum deles pode esquecer de pedi-los.

Moram em `~/.fireball/prompts.json` e não dentro do `settings.json` porque são
uma lista de textos de várias linhas, e não um punhado de escalares — misturar
os dois tornaria a configuração ilegível justamente onde editar na mão é o
caminho de quem não tem sessão gráfica. Qual deles é o padrão, esse sim mora
na configuração (`summary_prompt`), que é onde ficam as escolhas de uma vez só.

O arquivo pode não existir: nesse caso a lista é a dos embutidos (`SEEDS`), e
ninguém precisa salvar nada para o Fireball resumir.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from fireball import storage
from fireball.summarizers.prompt import DEFAULT_INSTRUCTIONS

BUILTIN_ID = "padrao"

MAX_NAME_LEN = 60

# Os prompts que existem antes de alguém salvar qualquer coisa. Não são
# "exemplos": são os formatos que uma gravação de reunião realmente pede, e
# estão aqui porque uma tela de prompts que começa com um item só não ensina o
# que dá pra fazer com ela. O primeiro é o padrão de fábrica.
#
# Todos descrevem só o campo "summary" — nome e tags saem do contrato fixo
# (ver `fireball.summarizers.prompt`), e nenhum deles precisa se lembrar disso.
SEEDS = [
    {
        "id": BUILTIN_ID,
        "name": "Padrão",
        "instructions": DEFAULT_INSTRUCTIONS.strip(),
    },
    {
        "id": "ata-formal",
        "name": "Ata formal",
        "instructions": """\
O markdown de "summary" é uma ata, nesta ordem:

<um parágrafo dizendo do que se tratou e quem conduziu>

## Pauta
- <cada assunto que entrou na conversa, na ordem em que apareceu>

## Deliberações
- <o que foi decidido, uma decisão por linha; omita a seção se nada foi decidido>

## Encaminhamentos
- <o que alguém ficou de fazer, e quem; omita a seção se não houver>

Registre também o que foi decidido *não* fazer — numa ata isso vale tanto
quanto o que foi aprovado. Não use primeira pessoa.\
""",
    },
    {
        "id": "so-as-acoes",
        "name": "Só as ações",
        "instructions": """\
O markdown de "summary" é só uma lista de tarefas, sem seções e sem parágrafo
de abertura:

- [ ] <o que fazer> — <quem, se der pra saber> — <prazo, se foi dito>

Nada de contexto, nada de resumo da conversa: quem lê isto quer saber o que
sobrou pra fazer. Uma linha por tarefa.

Se ninguém combinou nada de concreto, responda com uma única linha dizendo
isso, em vez de transformar comentário solto em tarefa. Frase no futuro ("a
gente devia…", "seria bom…") só vira tarefa se alguém assumiu.\
""",
    },
    {
        "id": "resumo-executivo",
        "name": "Resumo executivo",
        "instructions": """\
O markdown de "summary" tem no máximo **quatro linhas**, sem títulos e sem
seções — é para quem faltou e tem trinta segundos:

- <o ponto mais importante, primeiro>
- <o que mudou em relação ao que se sabia antes>
- <o que trava, se alguma coisa trava>
- <o que acontece a seguir>

Corte o que for procedimento, saudação e digressão. Se um dos pontos não
existe naquela reunião, escreva menos linhas em vez de encher.\
""",
    },
    {
        "id": "conversa-1-1",
        "name": "Conversa 1:1",
        "instructions": """\
O markdown de "summary" é a memória de uma conversa individual:

<um parágrafo com o clima geral e o que a pessoa trouxe>

## Temas
- <cada assunto levantado, com o que a pessoa disse sobre ele>

## Combinados
- <o que ficou acordado entre os dois; omita a seção se nada foi>

## Retomar na próxima
- <o que ficou pela metade, ou que pede acompanhamento; omita se não houver>

Preserve a forma como a pessoa descreveu o que sente — "está cansado do
projeto" não é a mesma coisa que "está desmotivado". Não suavize queixa nem
transforme desabafo em item de tarefa.\
""",
    },
    {
        "id": "entrevista",
        "name": "Entrevista / discovery",
        "instructions": """\
O markdown de "summary" organiza o que o entrevistado disse, não o que o
entrevistador perguntou:

<um parágrafo sobre quem é o entrevistado e o contexto dele, pelo que dá pra
saber da conversa>

## Dores
- <cada problema relatado, com a situação em que ele aparece>

## O que pediram
- <pedido explícito de funcionalidade ou mudança; omita a seção se não houver>

## Frases para citar
- "<citação literal, curta, que carrega a dor melhor que qualquer paráfrase>"

Em "Frases para citar", só transcreva o que dá pra ler com confiança — a
transcrição é automática, e citação inventada é pior que citação nenhuma. Não
proponha solução: aqui se registra o que foi ouvido.\
""",
    },
    {
        "id": "aula",
        "name": "Aula ou palestra",
        "instructions": """\
Esta gravação provavelmente é conteúdo assistido (aula, palestra, vídeo), e
não uma conversa: o áudio do sistema é quem fala, e o microfone só comenta.

O markdown de "summary":

<um parágrafo dizendo do que trata o conteúdo>

## Pontos
- <cada conceito ou passo apresentado, na ordem>

## Citado
- <ferramenta, livro, pessoa ou link mencionado; omita a seção se não houver>

## Meus comentários
- <o que quem gravou falou por cima, se falou; omita a seção se não houver>

Separar "Meus comentários" do resto é o que faz esta gravação valer depois:
o conteúdo se acha de novo, a reação de quem assistiu não.\
""",
    },
]


class UnknownPrompt(KeyError):
    """Id que não está na lista."""


class PromptInvalid(ValueError):
    """Nome ou instruções que não servem."""


def prompts_path():
    return storage.fireball_home() / "prompts.json"


def builtin() -> dict:
    """O padrão de fábrica — o texto que o botão "Texto padrão" devolve."""
    return dict(SEEDS[0])


def seeds() -> list[dict]:
    return [dict(seed) for seed in SEEDS]


def _clean(entry) -> Optional[dict]:
    """Uma entrada validada, ou None se ela não serve.

    Entrada estragada é ignorada em silêncio na leitura, pela mesma regra do
    `settings.load`: um prompts.json editado errado na mão não pode impedir o
    Fireball de resumir.
    """
    if not isinstance(entry, dict):
        return None
    prompt_id = slugify(entry.get("id"))
    name = re.sub(r"\s+", " ", str(entry.get("name") or "")).strip()[:MAX_NAME_LEN]
    instructions = str(entry.get("instructions") or "").strip()
    if not (prompt_id and name and instructions):
        return None
    return {"id": prompt_id, "name": name, "instructions": instructions}


def slugify(text) -> str:
    """Nome em id. Acento vira a letra sem acento, e não um corte: "Uma frase só"
    deve dar `uma-frase-so`, não `uma-frase-s`."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9-]+", "-", plain.strip().lower()).strip("-")[:40]


def load() -> list[dict]:
    """A lista salva, ou os embutidos quando não há arquivo (nem nada válido).

    Nunca devolve vazio: sem nenhum prompt não haveria o que escolher, e a
    tela de resumo ficaria oferecendo uma lista sem opção. Quem apagou um
    embutido não o vê voltar: apagar escreve o arquivo, e a partir daí é ele
    que manda.
    """
    stored = storage.read_json(prompts_path(), None)
    if not isinstance(stored, list):
        return seeds()

    prompts: list[dict] = []
    seen = set()
    for entry in stored:
        cleaned = _clean(entry)
        if cleaned and cleaned["id"] not in seen:
            seen.add(cleaned["id"])
            prompts.append(cleaned)
    return prompts or seeds()


def ids() -> list[str]:
    return [p["id"] for p in load()]


def get(prompt_id: str) -> dict:
    for prompt in load():
        if prompt["id"] == prompt_id:
            return prompt
    raise UnknownPrompt(f"Prompt de resumo desconhecido: '{prompt_id}'.")


def resolve(prompt_id: Optional[str]) -> dict:
    """O prompt a usar, com o primeiro da lista como último recurso.

    Id que não existe mais não pode fazer o resumo falhar: prompt apagado
    depois de a reunião ter sido resumida com ele é o caso normal, e resumir
    com outro é melhor que não resumir.
    """
    if prompt_id:
        try:
            return get(prompt_id)
        except UnknownPrompt:
            pass
    return load()[0]


def _unique_id(base: str, taken: set) -> str:
    prompt_id = base or "prompt"
    n = 2
    while prompt_id in taken:
        prompt_id = f"{base}-{n}"
        n += 1
    return prompt_id


def save(name: str, instructions: str, prompt_id: Optional[str] = None) -> dict:
    """Cria (sem `prompt_id`) ou reescreve um prompt. Devolve o que ficou salvo.

    Aqui, ao contrário do `load()`, texto inválido é erro alto: veio de alguém
    pedindo pra salvar, e salvar outra coisa em silêncio seria mentir.
    """
    cleaned = _clean({"id": prompt_id or slugify(name), "name": name, "instructions": instructions})
    if cleaned is None:
        if not re.sub(r"\s+", " ", str(name or "")).strip():
            raise PromptInvalid("O prompt precisa de um nome.")
        if not str(instructions or "").strip():
            raise PromptInvalid("O prompt precisa de instruções — sem elas não há o que pedir ao modelo.")
        raise PromptInvalid(f"Nome que não vira um id utilizável: {name!r}.")

    prompts = load()
    if not prompt_id:
        # Sem id, é sempre um prompt novo. Deixar o id sair do nome e ainda
        # aceitar colisão faria "Ata formal" salvo duas vezes apagar a primeira
        # — criar nunca pode destruir.
        cleaned["id"] = _unique_id(cleaned["id"], {p["id"] for p in prompts})
        prompts.append(cleaned)
    else:
        for i, existing in enumerate(prompts):
            if existing["id"] == cleaned["id"]:
                prompts[i] = cleaned
                break
        else:
            prompts.append(cleaned)  # apagado por outra janela no meio da edição

    _write(prompts)
    return cleaned


def delete(prompt_id: str) -> dict:
    """Apaga um prompt. O último não sai — sem nenhum não há o que escolher."""
    prompts = load()
    kept = [p for p in prompts if p["id"] != prompt_id]
    if len(kept) == len(prompts):
        raise UnknownPrompt(f"Prompt de resumo desconhecido: '{prompt_id}'.")
    if not kept:
        raise PromptInvalid("Este é o único prompt que existe — apagá-lo deixaria o resumo sem pedido.")
    _write(kept)
    return {"id": prompt_id, "deleted": True}


def _write(prompts: list[dict]) -> None:
    storage.write_json(prompts_path(), prompts)
