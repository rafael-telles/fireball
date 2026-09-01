"""Operações de arquivo de uma reunião: criar, mudar status, notas, ações.

**Chamado apenas pelo daemon** (`fireball.daemon.core`). CLI e GUI não usam
este módulo — elas falam com o daemon, que é o dono único do estado. Manter
essas funções sem lock nem noção de processo é de propósito: quem serializa o
acesso é o daemon, que roda tudo isso sob um lock só.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

from fireball import realtime, storage

# Status em que a reunião ainda está "viva" (o daemon tem — ou deveria ter —
# um processo cuidando dela). Fora daí, é estado terminal.
# 'paused' é vivo: o engine continua de pé e o microfone continua tomado por
# esta reunião, então ela ainda bloqueia começar outra.
LIVE_STATUSES = {"starting", "recording", "paused", "stopping"}


class MeetingAlreadyActive(RuntimeError):
    """Já existe uma reunião gravando — só uma por vez (mic e monitor do
    sistema são recursos exclusivos)."""


class MeetingBusy(RuntimeError):
    """A reunião está no meio de uma operação (gravando, finalizando) que
    impede o que foi pedido."""


class NoActiveMeeting(RuntimeError):
    """Pediram para parar/consultar a reunião ativa, e não há nenhuma."""


def create_meeting(
    name: Optional[str],
    fake: bool,
    mic_device: Optional[str],
    system_device: Optional[str],
    transcribe: bool,
    diarize: bool,
    backend: str,
    language: str,
    event: Optional[dict] = None,
    name_source: Optional[str] = None,
) -> dict:
    """Cria a pasta da reunião e o meeting.json inicial (status 'starting').

    Não sobe processo nenhum — quem faz isso é o daemon, que só promove para
    'recording' depois que o engine sobe de fato.

    **Nome é opcional, e o padrão é não ter.** Quem começa uma reunião está
    entrando nela: o nome bom só existe depois, quando se sabe do que ela foi.
    Então a reunião nasce sem nome e o provedor de resumo escreve um a partir
    da transcrição (ver `apply_summary_metadata`). Quem quiser nomear na hora
    ainda pode — e aí a IA não mexe.

    Começar a partir de um evento de agenda é o outro caminho: o daemon passa
    o snapshot em `event` e o título vira o nome com `name_source: "calendar"`.
    Qualquer fonte que não seja `"ai"` fica protegida do resumo (ver
    `apply_summary_metadata`).
    """
    name = (name or "").strip() or None
    if name_source is None:
        name_source = "user" if name else None
    meeting_dir = storage.new_meeting_dir(name)
    (meeting_dir / "notes.md").write_text(
        f"# {name or 'Notas da reunião'}\n\n_Notas ao vivo tomadas pelo Claude e por você._\n\n## Notas\n"
    )
    (meeting_dir / "transcript.ndjson").touch()

    meeting = {
        "id": meeting_dir.name,
        "name": name,
        # quem escreveu o nome: 'user' quando veio de gente, 'calendar'
        # quando veio do evento, 'ai' quando veio do provedor de resumo.
        # É o que impede o resumo regerado de passar por cima de um nome
        # que alguém digitou ou que a agenda já tinha.
        "name_source": name_source,
        # tags geradas junto com o resumo (ver apply_summary_metadata)
        "tags": [],
        "started_at": storage.now_iso(),
        "ended_at": None,
        "status": "starting",
        "fake": fake,
        "engine_pid": None,
        "exit_code": None,
        "mic_device": mic_device,
        "system_device": system_device,
        "transcribe_live": transcribe if not fake else None,
        "diarize": diarize if not fake else False,
        "backend": backend if not fake else None,
        "language": language if not fake else None,
    }
    if event:
        # snapshot datado do evento externo — local, descrição e convidados
        # moram aqui, não espalhados no topo do meeting.json
        meeting["event"] = event
    storage.write_json(meeting_dir / "meeting.json", meeting)
    storage.write_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    return meeting


def engine_command(
    meeting_id: str,
    fake: bool,
    interval: float,
    mic_device: Optional[str],
    system_device: Optional[str],
    transcribe: bool,
    diarize: bool,
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
            "--diarize" if diarize else "--no-diarize",
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


def summarize_command(meeting_id: str, provider: str, prompt: str = "") -> list[str]:
    cmd = [sys.executable, "-m", "fireball.cli", "_summarize", meeting_id, "--provider", provider]
    if prompt:
        cmd += ["--prompt", prompt]
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


def delete_meeting(meeting_id: str) -> dict:
    """Apaga a pasta da reunião inteira — áudio, transcrições, notas, tudo.

    Não há lixeira: o Fireball não tem para onde mover, e fingir que tem seria
    pior que dizer a verdade na hora de confirmar. Quem chama é o daemon, que
    já recusou o caso de reunião viva.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    if not meeting_dir.is_dir():
        raise FileNotFoundError(f"Reunião '{meeting_id}' não existe.")
    # confere que é mesmo uma pasta de reunião antes de apagar recursivamente:
    # um id inventado não pode virar rmtree em qualquer caminho.
    if not (meeting_dir / "meeting.json").exists():
        raise ValueError(f"'{meeting_id}' não parece uma reunião (sem meeting.json); nada foi apagado.")
    if meeting_dir.parent != storage.meetings_root():
        raise ValueError(f"'{meeting_id}' está fora da pasta de reuniões; nada foi apagado.")

    meeting = storage.read_json(meeting_dir / "meeting.json", {})
    shutil.rmtree(meeting_dir)
    return {"id": meeting_id, "name": meeting.get("name"), "deleted": True}


def get_meeting_status(meeting_id: str) -> dict:
    """Metadados + checkpoint + contagem de segmentos."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    checkpoint = storage.read_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    segments = sum(1 for _ in storage.read_ndjson(meeting_dir / TRANSCRIPT_FILE))
    return {
        "meeting": meeting,
        "checkpoint": checkpoint,
        "segments_total": segments,
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

# Uma reunião tem **uma** transcrição. Ela nasce do tempo real, enquanto a
# reunião grava, e o `finalize` a reescreve por cima com a versão feita sobre o
# áudio inteiro — que é mais precisa e é a mesma coisa, melhor.
#
# Antes eram dois arquivos convivendo, com um seletor na tela. Duas versões do
# mesmo texto significavam decidir em qual corrigir uma frase, e uma correção
# feita numa sumia quando a outra virava a exibida. Uma só remove a pergunta.
TRANSCRIPT_FILE = "transcript.ndjson"

# Nome antigo, mantido só para a migração de reuniões gravadas no modelo de dois
# arquivos (ver `migrate_transcripts`).
LEGACY_FINAL_FILE = "transcript_final.ndjson"


def transcript_path(meeting_id: str) -> Path:
    return storage.meeting_path(meeting_id) / TRANSCRIPT_FILE


def read_transcript(meeting_id: str, since_seq: int = 0) -> list[dict]:
    """Segmentos da transcrição com `seq` maior que `since_seq`.

    O corte por seq é o que deixa o chat ao vivo da GUI barato: ela guarda o
    último seq que já desenhou e a cada polling pede só o que veio depois, em
    vez de reler a reunião inteira e reconstruir a tela.
    """
    return list(storage.read_ndjson(transcript_path(meeting_id), since_seq=since_seq))


def _rewrite_transcript(meeting_id: str, change) -> dict:
    """Reescreve a transcrição aplicando `change` a cada segmento.

    `change` devolve o segmento novo, ou None para descartá-lo. A escrita vai
    num temporário e troca por cima (`replace`, atômico no mesmo filesystem):
    o ndjson é lido por outros processos, e uma troca parcial deixaria a
    transcrição ilegível no meio da leitura.
    """
    path = transcript_path(meeting_id)
    if not path.exists():
        raise FileNotFoundError(f"A reunião '{meeting_id}' não tem transcrição.")

    touched = None
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as out:
        for seg in storage.read_ndjson(path):
            new = change(seg)
            if new is not seg:
                touched = new or seg
            if new is not None:
                out.write(json.dumps(new, ensure_ascii=False) + "\n")

    if touched is None:
        tmp.unlink(missing_ok=True)
        raise LookupError("Segmento não encontrado na transcrição.")
    tmp.replace(path)
    return touched


def edit_segment(meeting_id: str, seq: int, text: str) -> dict:
    """Corrige o texto de um segmento já transcrito.

    Corrige **só a transcrição** — o áudio original não muda, e por isso o
    segmento fica marcado com `edited`. Sem essa marca, uma transcrição
    corrigida seria indistinguível do que o motor de fato ouviu, e quem lesse
    depois não teria como saber que aquilo é revisão humana.
    """
    text = text.strip()
    if not text:
        raise ValueError("O texto do segmento não pode ficar vazio.")

    def change(seg):
        return {**seg, "text": text, "edited": True} if seg.get("seq") == seq else seg

    return _rewrite_transcript(meeting_id, change)


def delete_segment(meeting_id: str, seq: int) -> dict:
    """Tira um segmento da transcrição de vez.

    Os `seq` dos demais **não** são renumerados: eles são a identidade de cada
    fala, e o chat da janela pede "o que veio depois do seq N" a cada volta do
    polling. Renumerar mudaria o significado de um número que a tela já tem na
    mão. Buraco na sequência é esperado.
    """
    removed = {}

    def change(seg):
        if seg.get("seq") != seq:
            return seg
        removed.update(seg)
        return None

    _rewrite_transcript(meeting_id, change)
    return {"seq": seq, "deleted": True, "text": removed.get("text")}


def migrate_transcripts() -> list[str]:
    """Funde o modelo antigo de dois arquivos no de um só.

    Reuniões gravadas antes disso têm `transcript.ndjson` (tempo real) e talvez
    `transcript_final.ndjson`. A final é a mesma transcrição, melhor — que é
    exatamente o que o `finalize` passa a fazer por cima do arquivo único.
    Então ela vira a transcrição, e o nome antigo some.

    Final vazia (finalize que falhou no meio) é descartada em vez de promovida:
    trocar uma transcrição que existe por um arquivo vazio perderia a reunião.
    """
    migrated = []
    root = storage.meetings_root()
    if not root.is_dir():
        return migrated
    for meeting_dir in sorted(root.iterdir()):
        legacy = meeting_dir / LEGACY_FINAL_FILE
        if not legacy.is_file():
            continue
        if any(True for _ in storage.read_ndjson(legacy)):
            legacy.replace(meeting_dir / TRANSCRIPT_FILE)
            migrated.append(meeting_dir.name)
        else:
            legacy.unlink()
    return migrated


# ------------------------------------------------------------------ avisos

# O engine grava esses arquivos quando algo deu errado mas a gravação seguiu
# assim mesmo — são exatamente as falhas que passam despercebidas, porque
# nada quebra na hora: você só descobre depois que faltou metade da reunião.
WARNING_FILES = (
    ("audio", "audio_warnings.log"),
    ("transcricao", "transcribe_warnings.log"),
    ("diarizacao", "diarization_warnings.log"),
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
    """Onde está o áudio da reunião.

    `meeting.wav` é a mistura de mic + sistema, escrita pelo engine ao terminar
    de gravar. Quando o monitor do sistema não abriu, ela não existe — mas
    mic.wav existe, e ouvir só o seu lado é melhor que não ouvir nada.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    for name, mixed in (("meeting.wav", True), ("mic.wav", False)):
        path = meeting_dir / name
        if path.exists():
            return {"path": str(path), "exists": True, "size": path.stat().st_size, "mixed": mixed}
    return {"path": None, "exists": False, "size": 0, "mixed": False}


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
                "segments": sum(1 for _ in storage.read_ndjson(meeting_dir / TRANSCRIPT_FILE)),
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

    Renomear marca a reunião como nomeada por gente (`name_source: "user"`), e
    isso é definitivo: regerar o resumo não desfaz. Um nome escolhido à mão
    sendo trocado por um de modelo, sem ninguém pedir, seria perder trabalho de
    quem estava na reunião.
    """
    name = name.strip()
    if not name:
        raise ValueError("O nome da reunião não pode ficar vazio.")
    return write_meeting_fields(meeting_id, name=name, name_source="user")


def apply_summary_metadata(meeting_id: str) -> dict:
    """Passa para o meeting.json o nome e as tags que saíram com o resumo.

    Chamado **pelo daemon**, quando o job de resumo sai com código 0 — o
    processo filho só escreve o resultado em `summary_result.json`, quem muda
    estado é o dono dele.

    Duas regras diferentes de propósito:

    - **nome**: só entra se a reunião ainda não tem um dado por gente. Uma
      reunião nasce sem nome justamente para este momento; se alguém já
      digitou um, ele vale mais que qualquer sugestão de modelo.
    - **tags**: substituem as anteriores. Elas são derivadas da transcrição,
      como o resumo — manter tag de uma versão antiga ao lado das novas seria
      mostrar duas leituras da mesma reunião como se fossem uma.
    - **prompt**: fica gravado o que de fato escreveu este resumo. É o que faz
      regerar (e o resumo automático depois do finalize) repetir a escolha em
      vez de cair no padrão e trocar de formato no meio do caminho.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    result = storage.read_json(meeting_dir / "summary_result.json", {}) or {}
    meeting = read_meeting(meeting_id)

    fields = {}
    prompt_id = (result.get("prompt") or "").strip()
    if prompt_id:
        fields["summary_prompt"] = prompt_id

    tags = [tag for tag in (result.get("tags") or []) if tag]
    if tags:
        fields["tags"] = tags

    title = (result.get("title") or "").strip()
    named_by_user = bool((meeting.get("name") or "").strip()) and meeting.get("name_source") != "ai"
    if title and not named_by_user:
        fields["name"] = title
        fields["name_source"] = "ai"

    return write_meeting_fields(meeting_id, **fields) if fields else meeting


def append_note(meeting_id: str, text: str, author: str, stamp: str) -> None:
    notes_path = storage.meeting_path(meeting_id) / "notes.md"
    tag = "🤖" if author == "claude" else "🧑"
    with notes_path.open("a") as f:
        f.write(f"- `{stamp}` {tag} {text}\n")
