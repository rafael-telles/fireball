"""Operações de arquivo de uma reunião: criar, mudar status, notas, ações.

**Chamado apenas pelo daemon** (`fireball.daemon.core`). CLI e GUI não usam
este módulo — elas falam com o daemon, que é o dono único do estado. Manter
essas funções sem lock nem noção de processo é de propósito: quem serializa o
acesso é o daemon, que roda tudo isso sob um lock só.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from fireball import realtime, storage

# Status em que a reunião ainda está "viva" (o daemon tem — ou deveria ter —
# um processo cuidando dela). Fora daí, é estado terminal.
LIVE_STATUSES = {"starting", "recording", "stopping"}


class MeetingAlreadyActive(RuntimeError):
    """Já existe uma reunião gravando — só uma por vez (mic e monitor do
    sistema são recursos exclusivos)."""


class MeetingBusy(RuntimeError):
    """A reunião está no meio de uma operação (gravando, finalizando) que
    impede o que foi pedido."""


class NoActiveMeeting(RuntimeError):
    """Pediram para parar/consultar a reunião ativa, e não há nenhuma."""


def create_meeting(
    name: str,
    fake: bool,
    mic_device: Optional[str],
    system_device: Optional[str],
    transcribe: bool,
    backend: str,
    language: str,
) -> dict:
    """Cria a pasta da reunião e o meeting.json inicial (status 'starting').

    Não sobe processo nenhum — quem faz isso é o daemon, que só promove para
    'recording' depois que o engine sobe de fato.
    """
    meeting_dir = storage.new_meeting_dir(name)
    (meeting_dir / "notes.md").write_text(
        f"# {name}\n\n_Notas ao vivo tomadas pelo Claude e por você._\n\n## Notas\n"
    )
    (meeting_dir / "transcript.ndjson").touch()

    meeting = {
        "id": meeting_dir.name,
        "name": name,
        "started_at": storage.now_iso(),
        "ended_at": None,
        "status": "starting",
        "fake": fake,
        "engine_pid": None,
        "exit_code": None,
        "mic_device": mic_device,
        "system_device": system_device,
        "transcribe_live": transcribe if not fake else None,
        "backend": backend if not fake else None,
        "language": language if not fake else None,
    }
    storage.write_json(meeting_dir / "meeting.json", meeting)
    storage.write_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    storage.write_json(meeting_dir / "actions.json", [])
    return meeting


def engine_command(
    meeting_id: str,
    fake: bool,
    interval: float,
    mic_device: Optional[str],
    system_device: Optional[str],
    transcribe: bool,
    backend: str,
    language: str,
) -> list[str]:
    """Linha de comando do processo de engine que o daemon vai supervisionar."""
    cmd = [
        sys.executable,
        "-m",
        "fireball.cli",
        "_engine",
        meeting_id,
        "--fake" if fake else "--real",
        "--interval",
        str(interval),
    ]
    if mic_device is not None:
        cmd += ["--mic-device", mic_device]
    if system_device is not None:
        cmd += ["--system-device", system_device]
    if not fake:
        cmd += [
            "--transcribe" if transcribe else "--no-transcribe",
            "--backend",
            backend,
            "--language",
            language,
        ]
    return cmd


def finalize_command(meeting_id: str, backend: Optional[str]) -> list[str]:
    cmd = [sys.executable, "-m", "fireball.cli", "_finalize", meeting_id]
    if backend:
        cmd += ["--backend", backend]
    return cmd


def summarize_command(meeting_id: str, provider: str) -> list[str]:
    return [sys.executable, "-m", "fireball.cli", "_summarize", meeting_id, "--provider", provider]


def read_meeting(meeting_id: str) -> dict:
    return storage.read_json(storage.meeting_path(meeting_id) / "meeting.json")


def update_meeting(meeting_id: str, **fields) -> dict:
    """Aplica campos ao meeting.json. Se `status` virar terminal e ainda não
    houver `ended_at`, carimba o fim aqui — assim nenhum caminho de saída
    esquece de fechar a reunião."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    meeting.update(fields)
    status = meeting.get("status")
    if status not in LIVE_STATUSES and status != "finalizing" and not meeting.get("ended_at"):
        meeting["ended_at"] = storage.now_iso()
    storage.write_json(meeting_dir / "meeting.json", meeting)
    return meeting


def get_meeting_status(meeting_id: str) -> dict:
    """Metadados + checkpoint + contagem de segmentos/ações pendentes."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    checkpoint = storage.read_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    segments = sum(1 for _ in storage.read_ndjson(meeting_dir / "transcript.ndjson"))
    actions = storage.read_json(meeting_dir / "actions.json", [])
    pending = [a for a in actions if a["status"] == "pending"]
    return {
        "meeting": meeting,
        "checkpoint": checkpoint,
        "segments_total": segments,
        "actions_pending": len(pending),
        # a ficha da reunião mostra a pasta e oferece abrir ela; quem sabe onde
        # os arquivos moram é este módulo, não a janela
        "path": str(meeting_dir),
    }


def list_meetings() -> list[dict]:
    """Todas as reuniões conhecidas, mais recentes por último (ordem de pasta)."""
    rows = []
    for d in sorted(storage.meetings_root().iterdir()):
        if d.is_dir():
            rows.append(storage.read_json(d / "meeting.json", {"id": d.name}))
    return rows


def scan_live_meetings() -> list[dict]:
    """Reuniões que o filesystem ainda dá como vivas.

    Usado **só na subida do daemon**, para reconciliar o que sobrou de uma
    vida anterior (adotar o engine órfão ou marcar como 'crashed'). Em regime
    normal a reunião ativa vive na memória do daemon, não aqui.
    """
    return [m for m in list_meetings() if m.get("status") in LIVE_STATUSES]


# ------------------------------------------------------------- transcrição

# Duas transcrições convivem por reunião: a do tempo real (escrita pelo engine
# durante a gravação) e a final (escrita pelo `finalize`, mais precisa). A GUI
# mostra as duas — ao vivo só existe a primeira; numa reunião passada já
# finalizada, a final é o padrão.
TRANSCRIPT_FILES = {"realtime": "transcript.ndjson", "final": "transcript_final.ndjson"}


def read_transcript(meeting_id: str, since_seq: int = 0, source: str = "realtime") -> list[dict]:
    """Segmentos da transcrição com `seq` maior que `since_seq`.

    O corte por seq é o que deixa o chat ao vivo da GUI barato: ela guarda o
    último seq que já desenhou e a cada polling pede só o que veio depois, em
    vez de reler a reunião inteira e reconstruir a tela.
    """
    filename = TRANSCRIPT_FILES.get(source)
    if filename is None:
        raise ValueError(f"Fonte de transcrição desconhecida: {source!r} (use realtime ou final).")
    return list(storage.read_ndjson(storage.meeting_path(meeting_id) / filename, since_seq=since_seq))


def edit_segment(meeting_id: str, seq: int, text: str, source: str = "realtime") -> dict:
    """Corrige o texto de um segmento já transcrito.

    Reescreve o arquivo inteiro num temporário e troca por cima (`replace`, que
    é atômico no mesmo filesystem): o ndjson é lido por outros processos, e uma
    troca parcial deixaria a transcrição ilegível no meio da leitura.

    Corrige **só a transcrição** — o áudio original não muda, e por isso o
    segmento fica marcado com `edited`. Sem essa marca, uma transcrição
    corrigida seria indistinguível do que o motor de fato ouviu, e quem lesse
    depois não teria como saber que aquilo é revisão humana.
    """
    filename = TRANSCRIPT_FILES.get(source)
    if filename is None:
        raise ValueError(f"Fonte de transcrição desconhecida: {source!r} (use realtime ou final).")

    path = storage.meeting_path(meeting_id) / filename
    if not path.exists():
        raise FileNotFoundError(f"A reunião '{meeting_id}' não tem transcrição em '{source}'.")

    text = text.strip()
    if not text:
        raise ValueError("O texto do segmento não pode ficar vazio.")

    updated = None
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as out:
        for seg in storage.read_ndjson(path):
            if seg.get("seq") == seq:
                seg = {**seg, "text": text, "edited": True}
                updated = seg
            out.write(json.dumps(seg, ensure_ascii=False) + "\n")

    if updated is None:
        tmp.unlink(missing_ok=True)
        raise LookupError(f"Segmento {seq} não existe na transcrição '{source}'.")

    tmp.replace(path)
    return updated


# ------------------------------------------------------------------ avisos

# O engine grava esses arquivos quando algo deu errado mas a gravação seguiu
# assim mesmo — são exatamente as falhas que passam despercebidas, porque
# nada quebra na hora: você só descobre depois que faltou metade da reunião.
WARNING_FILES = (
    ("audio", "audio_warnings.log"),
    ("transcricao", "transcribe_warnings.log"),
)


def read_warnings(meeting_id: str) -> list[dict]:
    warnings = []
    meeting_dir = storage.meeting_path(meeting_id)
    for kind, filename in WARNING_FILES:
        path = meeting_dir / filename
        if not path.exists():
            continue
        text = path.read_text().strip()
        if text:
            warnings.append({"kind": kind, "file": filename, "text": text})
    return warnings


# ------------------------------------------------------------------ resumo


def read_summary(meeting_id: str) -> Optional[dict]:
    """O resumo gerado, com a procedência — ou None se ainda não houver."""
    meeting_dir = storage.meeting_path(meeting_id)
    path = meeting_dir / "summary.md"
    if not path.exists():
        return None
    return {
        "markdown": path.read_text(),
        **storage.read_json(meeting_dir / "summary_result.json", {}),
    }


# ------------------------------------------------------------------- áudio


def audio_info(meeting_id: str) -> dict:
    """Onde está o áudio da reunião, e se dá pra acompanhar a transcrição nele.

    `meeting.wav` é a mistura de mic + sistema que o finalize produz. Só a
    transcrição **final** carrega deslocamento em segundos (`start`/`end`); a
    do tempo real tem carimbo de relógio absoluto, que não dá posição dentro do
    arquivo. Por isso o player só sincroniza na final, e a janela precisa saber
    disso daqui em vez de adivinhar.
    """
    path = storage.meeting_path(meeting_id) / "meeting.wav"
    exists = path.exists()
    return {
        "path": str(path) if exists else None,
        "exists": exists,
        "size": path.stat().st_size if exists else 0,
    }


def meeting_summaries() -> list[dict]:
    """`list_meetings()` mais o que cada linha da lista da GUI precisa mostrar.

    Mais recentes primeiro: o id começa com o timestamp, então inverter a
    ordem alfabética já é ordem cronológica reversa — que é como se olha
    histórico.
    """
    rows = []
    for meeting in reversed(list_meetings()):
        meeting_dir = storage.meetings_root() / meeting["id"]
        rows.append(
            {
                **meeting,
                "segments": sum(1 for _ in storage.read_ndjson(meeting_dir / "transcript.ndjson")),
                "has_final": (meeting_dir / "transcript_final.ndjson").exists(),
            }
        )
    return rows


# ------------------------------------------------------------------- notas


def read_notes(meeting_id: str) -> str:
    """O notes.md inteiro, como texto — é o que o editor da janela abre."""
    path = storage.meeting_path(meeting_id) / "notes.md"
    return path.read_text() if path.exists() else ""


def write_notes(meeting_id: str, text: str) -> None:
    """Substitui o notes.md inteiro pelo que está no editor.

    Diferente de `append_note`, que acrescenta uma linha carimbada vinda do
    Claude ou da CLI: aqui quem escreve é a pessoa, editando o arquivo na
    janela, e o que está na tela é a versão boa. As duas escritas convivem
    porque ambas passam pelo lock do daemon — mas uma nota que chegue enquanto
    o editor está aberto só aparece no próximo carregamento.
    """
    (storage.meeting_path(meeting_id) / "notes.md").write_text(text)


def write_meeting_fields(meeting_id: str, **fields) -> dict:
    """Aplica campos ao meeting.json **sem** mexer no ciclo de vida.

    Existe separado de `update_meeting` porque aquele carimba `ended_at`
    quando o status é terminal e o campo está vazio. Isso é certo para quem
    está encerrando a reunião e errado para todo o resto: renomear ou anotar
    que o resumo terminou daria a uma reunião quebrada um fim inventado — a
    hora em que alguém mexeu nela. Só quem muda `status` deve usar
    `update_meeting`.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    meeting.update(fields)
    storage.write_json(meeting_dir / "meeting.json", meeting)
    return meeting


def rename_meeting(meeting_id: str, name: str) -> dict:
    """Troca só o nome de exibição.

    A pasta continua com o slug do nome original: o id é a identidade, e mexer
    nele quebraria todo caminho já gravado.
    """
    name = name.strip()
    if not name:
        raise ValueError("O nome da reunião não pode ficar vazio.")
    return write_meeting_fields(meeting_id, name=name)


def append_note(meeting_id: str, text: str, author: str, stamp: str) -> None:
    notes_path = storage.meeting_path(meeting_id) / "notes.md"
    tag = "🤖" if author == "claude" else "🧑"
    with notes_path.open("a") as f:
        f.write(f"- `{stamp}` {tag} {text}\n")


def add_action(meeting_id: str, title: str, detail: str, system: str) -> dict:
    actions_path = storage.meeting_path(meeting_id) / "actions.json"
    actions = storage.read_json(actions_path, [])
    entry = {
        "id": f"a{len(actions) + 1}",
        "title": title,
        "detail": detail,
        "system": system,
        "status": "pending",
        "created_at": storage.now_iso(),
    }
    actions.append(entry)
    storage.write_json(actions_path, actions)
    return entry


def list_actions(meeting_id: str, status_filter: str = "all") -> list[dict]:
    actions = storage.read_json(storage.meeting_path(meeting_id) / "actions.json", [])
    if status_filter != "all":
        actions = [a for a in actions if a["status"] == status_filter]
    return actions


def set_action_status(meeting_id: str, action_id: str, new_status: str) -> dict:
    actions_path = storage.meeting_path(meeting_id) / "actions.json"
    actions = storage.read_json(actions_path, [])
    for a in actions:
        if a["id"] == action_id:
            a["status"] = new_status
            storage.write_json(actions_path, actions)
            return a
    raise KeyError(f"Ação '{action_id}' não encontrada na reunião '{meeting_id}'.")
