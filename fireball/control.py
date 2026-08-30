"""Operações de arquivo de uma reunião: criar, mudar status, notas, ações.

**Chamado apenas pelo daemon** (`fireball.daemon.core`). CLI e GUI não usam
este módulo — elas falam com o daemon, que é o dono único do estado. Manter
essas funções sem lock nem noção de processo é de propósito: quem serializa o
acesso é o daemon, que roda tudo isso sob um lock só.
"""

from __future__ import annotations

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


def rename_meeting(meeting_id: str, name: str) -> dict:
    """Troca só o nome de exibição.

    Não passa por `update_meeting` de propósito: aquele carimba `ended_at`
    quando o status é terminal e o campo está vazio, o que daria a uma reunião
    quebrada um fim inventado — a hora em que alguém a renomeou. Renomear não
    é um evento do ciclo de vida da reunião.

    A pasta continua com o slug do nome original: o id é a identidade, e mexer
    nele quebraria todo caminho já gravado.
    """
    name = name.strip()
    if not name:
        raise ValueError("O nome da reunião não pode ficar vazio.")
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    meeting["name"] = name
    storage.write_json(meeting_dir / "meeting.json", meeting)
    return meeting


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
