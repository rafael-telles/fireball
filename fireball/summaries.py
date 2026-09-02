"""Os resumos de uma reunião — um por prompt, guardados lado a lado.

Um resumo é uma leitura da transcrição, e prompts diferentes leem coisas
diferentes: a ata formal e o "para quem não estava" são as duas úteis, não uma
melhor que a outra. Então gerar com outro prompt **acrescenta**; só regerar com
o mesmo prompt substitui, que é o que "regerar" quer dizer.

O arquivo por prompt mora em `summaries/<prompt_id>.md`, com a procedência
ao lado em `.json`. `summary.md` na raiz da pasta continua existindo e
continua sendo o último gerado: é o que a CLI, as skills e o índice de busca
leem, e mudar isso quebraria quem já depende dele.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fireball import storage

STORE_DIR = "summaries"
CURRENT_MD = "summary.md"
CURRENT_JSON = "summary_result.json"


def store_dir(meeting_dir: Path) -> Path:
    return meeting_dir / STORE_DIR


def _slug(prompt_id: str) -> str:
    """Nome de arquivo a partir do id do prompt.

    Os ids já saem de `prompts.slugify`, mas este módulo lê o que está no
    disco: um id estranho vindo de um JSON editado à mão não pode virar
    caminho para fora da pasta da reunião.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (prompt_id or "").strip())
    return safe.strip("-") or "sem-prompt"


def write(meeting_dir: Path, markdown: str, result: dict) -> dict:
    """Grava o resumo nos dois lugares: o do prompt e o "último gerado"."""
    body = markdown.rstrip("\n") + "\n"
    (meeting_dir / CURRENT_MD).write_text(body)
    storage.write_json(meeting_dir / CURRENT_JSON, result)

    folder = store_dir(meeting_dir)
    folder.mkdir(parents=True, exist_ok=True)
    slug = _slug(result.get("prompt") or "")
    (folder / f"{slug}.md").write_text(body)
    storage.write_json(folder / f"{slug}.json", {**result, "id": slug})
    return result


def drop_prompt(meeting_dir: Path, prompt_id: str) -> None:
    """Tira de cena o resumo daquele prompt — é o que regerar faz.

    Só o daquele prompt: os outros são resumos de outra leitura, e uma
    geração que falhe no meio não pode levá-los junto.
    """
    slug = _slug(prompt_id)
    folder = store_dir(meeting_dir)
    (folder / f"{slug}.md").unlink(missing_ok=True)
    (folder / f"{slug}.json").unlink(missing_ok=True)


def listing(meeting_dir: Path) -> list[dict]:
    """Os resumos gravados, mais recente primeiro, com o markdown de cada um.

    Inclui o `summary.md` da raiz quando ele é de um prompt que ainda não tem
    arquivo próprio — é o caso das reuniões resumidas antes de existir a pasta
    `summaries/`, e elas não podem sumir da tela por isso.
    """
    entries: dict[str, dict] = {}
    folder = store_dir(meeting_dir)
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            entry = _entry(path.with_suffix(".json"), path, path.stem)
            if entry:
                entries[entry["id"]] = entry

    legacy = _entry(meeting_dir / CURRENT_JSON, meeting_dir / CURRENT_MD, None)
    if legacy and legacy["id"] not in entries:
        entries[legacy["id"]] = legacy

    return sorted(entries.values(), key=lambda e: e.get("generated_at") or "", reverse=True)


def get(meeting_dir: Path, summary_id: str) -> Optional[dict]:
    slug = _slug(summary_id)
    for entry in listing(meeting_dir):
        if entry["id"] == slug:
            return entry
    return None


def delete(meeting_dir: Path, summary_id: str) -> dict:
    """Apaga um resumo. Se era o "último gerado", a raiz vai com ele."""
    slug = _slug(summary_id)
    folder = store_dir(meeting_dir)
    (folder / f"{slug}.md").unlink(missing_ok=True)
    (folder / f"{slug}.json").unlink(missing_ok=True)

    current = storage.read_json(meeting_dir / CURRENT_JSON, {}) or {}
    if _slug(current.get("prompt") or "") == slug:
        (meeting_dir / CURRENT_MD).unlink(missing_ok=True)
        (meeting_dir / CURRENT_JSON).unlink(missing_ok=True)
        remaining = listing(meeting_dir)
        # a raiz não pode ficar vazia enquanto ainda há resumo: é dela que a
        # CLI e o índice de busca leem
        if remaining:
            newest = remaining[0]
            (meeting_dir / CURRENT_MD).write_text(newest["markdown"])
            storage.write_json(
                meeting_dir / CURRENT_JSON,
                {key: value for key, value in newest.items() if key != "markdown"},
            )
    return {"deleted": slug}


def _entry(json_path: Path, md_path: Path, slug: Optional[str]) -> Optional[dict]:
    if not md_path.exists():
        return None
    result = storage.read_json(json_path, {}) or {}
    return {
        **result,
        "id": slug or _slug(result.get("prompt") or ""),
        "markdown": md_path.read_text(),
    }
