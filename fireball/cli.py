"""CLI do Fireball.

Este CLI é a tool que o Claude usa para agir como escrivão de uma reunião:
iniciar/parar gravação, acompanhar a transcrição ao vivo, tomar notas e
gerenciar ações pendentes de aprovação. Ver skills/fireball/SKILL.md para
o fluxo completo que o Claude segue.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import click

from fireball import audio, engine, realtime, storage
from fireball.backends import BATCH_BACKENDS, REALTIME_BACKENDS, BackendUnavailable, get_batch_backend


@click.group()
def cli():
    """Fireball — grava e transcreve reuniões, servindo de tools para o Claude atuar como escrivão."""


@cli.command()
def devices():
    """Lista as fontes de áudio disponíveis (mics e monitores de saída), via pactl.

    Use o campo "name" com --mic-device/--system-device em `fireball start`
    quando o padrão (@DEFAULT_SOURCE@ / @DEFAULT_MONITOR@) não for o que você
    quer — por exemplo, um headset bluetooth específico.
    """
    for source in audio.list_sources():
        click.echo(json.dumps(source, ensure_ascii=False))


@cli.command()
@click.option("--name", default="Reunião", help="Nome da reunião.")
@click.option(
    "--fake/--real",
    default=True,
    help="Usa o motor simulado (transcrição fake) ou grava áudio real (mic + sistema).",
)
@click.option("--interval", default=3.0, type=float, help="Intervalo em segundos entre segmentos (modo --fake).")
@click.option(
    "--mic-device",
    default=None,
    help="Nome da fonte de microfone (ver `fireball devices`). Padrão: @DEFAULT_SOURCE@ (entrada padrão do sistema).",
)
@click.option(
    "--system-device",
    default=None,
    help="Nome da fonte de monitor do sistema (ver `fireball devices`). Padrão: @DEFAULT_MONITOR@ (monitor da saída padrão).",
)
@click.option(
    "--transcribe/--no-transcribe",
    default=True,
    help="No modo --real, roda transcrição em tempo real local em paralelo à gravação.",
)
@click.option(
    "--backend",
    type=click.Choice(list(REALTIME_BACKENDS)),
    default="whisper",
    help="Motor de transcrição ao vivo: 'whisper' (faster-whisper, multi-idioma) ou 'parakeet' (NeMo Parakeet TDT via ONNX).",
)
@click.option(
    "--language", default=realtime.DEFAULT_LANGUAGE, help="Idioma esperado da fala (código curto, ex: pt, en)."
)
def start(name, fake, interval, mic_device, system_device, transcribe, backend, language):
    """Inicia uma nova reunião: cria a pasta e sobe o motor de gravação/transcrição em background."""
    meeting_dir = storage.new_meeting_dir(name)
    meeting_id = meeting_dir.name

    (meeting_dir / "notes.md").write_text(
        f"# {name}\n\n_Notas ao vivo tomadas pelo Claude e por você._\n\n## Notas\n"
    )
    (meeting_dir / "transcript.ndjson").touch()

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
        cmd += ["--transcribe" if transcribe else "--no-transcribe", "--backend", backend, "--language", language]

    log_path = meeting_dir / "engine.log"
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    meeting = {
        "id": meeting_id,
        "name": name,
        "started_at": storage.now_iso(),
        "ended_at": None,
        "status": "recording",
        "fake": fake,
        "engine_pid": proc.pid,
        "mic_device": mic_device,
        "system_device": system_device,
        "transcribe_live": transcribe if not fake else None,
        "backend": backend if not fake else None,
        "language": language if not fake else None,
    }
    storage.write_json(meeting_dir / "meeting.json", meeting)
    storage.write_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    storage.write_json(meeting_dir / "actions.json", [])

    click.echo(json.dumps({"meeting_id": meeting_id, "path": str(meeting_dir)}, ensure_ascii=False))


@cli.command(name="_engine", hidden=True)
@click.argument("meeting_id")
@click.option("--fake/--real", default=True)
@click.option("--interval", default=3.0, type=float)
@click.option("--mic-device", default=None)
@click.option("--system-device", default=None)
@click.option("--transcribe/--no-transcribe", default=True)
@click.option("--backend", type=click.Choice(list(REALTIME_BACKENDS)), default="whisper")
@click.option("--language", default=realtime.DEFAULT_LANGUAGE)
def _engine_cmd(meeting_id, fake, interval, mic_device, system_device, transcribe, backend, language):
    meeting_dir = storage.meeting_path(meeting_id)
    if fake:
        engine.run_fake_engine(meeting_dir, interval=interval)
    else:
        engine.run_real_engine(
            meeting_dir,
            mic_device=mic_device,
            system_device=system_device,
            transcribe_live=transcribe,
            backend_name=backend,
            language=language,
        )


@cli.command()
@click.argument("meeting_id")
def stop(meeting_id):
    """Para a gravação/transcrição em tempo real de uma reunião."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    pid = meeting.get("engine_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    meeting["status"] = "stopped"
    meeting["ended_at"] = storage.now_iso()
    storage.write_json(meeting_dir / "meeting.json", meeting)
    click.echo(json.dumps(meeting, ensure_ascii=False))


@cli.command()
@click.argument("meeting_id")
def status(meeting_id):
    """Mostra o estado atual de uma reunião: metadados, checkpoint, segmentos, ações pendentes."""
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    checkpoint = storage.read_json(meeting_dir / "checkpoint.json", {"last_seq": 0})
    segments = list(storage.read_ndjson(meeting_dir / "transcript.ndjson"))
    actions = storage.read_json(meeting_dir / "actions.json", [])
    pending = [a for a in actions if a["status"] == "pending"]
    click.echo(
        json.dumps(
            {
                "meeting": meeting,
                "checkpoint": checkpoint,
                "segments_total": len(segments),
                "actions_pending": len(pending),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@cli.command(name="list")
def list_meetings():
    """Lista todas as reuniões conhecidas."""
    rows = []
    for d in sorted(storage.meetings_root().iterdir()):
        if d.is_dir():
            rows.append(storage.read_json(d / "meeting.json", {"id": d.name}))
    click.echo(json.dumps(rows, ensure_ascii=False, indent=2))


@cli.group()
def transcript():
    """Consulta ou acompanha a transcrição de uma reunião."""


@transcript.command(name="show")
@click.argument("meeting_id")
@click.option("--since", default=0, type=int, help="Só mostra segmentos com seq maior que este valor.")
@click.option("--source", type=click.Choice(["realtime", "final"]), default="realtime")
def transcript_show(meeting_id, since, source):
    meeting_dir = storage.meeting_path(meeting_id)
    filename = "transcript.ndjson" if source == "realtime" else "transcript_final.ndjson"
    for seg in storage.read_ndjson(meeting_dir / filename, since_seq=since):
        click.echo(json.dumps(seg, ensure_ascii=False))


@transcript.command(name="follow")
@click.argument("meeting_id")
@click.option("--since", default=None, type=int, help="Retoma a partir deste seq (padrão: usa o checkpoint salvo).")
@click.option("--poll", default=0.5, type=float, help="Intervalo de polling do arquivo, em segundos.")
def transcript_follow(meeting_id, since, poll):
    """Acompanha a transcrição em tempo real, uma linha JSON por segmento novo.

    Pensado para rodar via Bash em background e ser observado com o Monitor tool:
    cada linha de stdout é um segmento novo da reunião. Termina sozinho quando a
    reunião não está mais 'recording' e não há mais segmentos novos.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    transcript_path = meeting_dir / "transcript.ndjson"
    checkpoint_path = meeting_dir / "checkpoint.json"

    last_seq = since if since is not None else storage.read_json(checkpoint_path, {"last_seq": 0})["last_seq"]

    while True:
        meeting = storage.read_json(meeting_dir / "meeting.json", {})
        new_any = False
        for seg in storage.read_ndjson(transcript_path, since_seq=last_seq):
            click.echo(json.dumps(seg, ensure_ascii=False))
            sys.stdout.flush()
            last_seq = seg["seq"]
            new_any = True
        if meeting.get("status") != "recording" and not new_any:
            break
        time.sleep(poll)


@transcript.command(name="ack")
@click.argument("meeting_id")
@click.argument("seq", type=int)
def transcript_ack(meeting_id, seq):
    """Marca até onde o Claude já processou o stream (para retomar após reconexão)."""
    meeting_dir = storage.meeting_path(meeting_id)
    storage.write_json(meeting_dir / "checkpoint.json", {"last_seq": seq})
    click.echo(json.dumps({"last_seq": seq}))


@cli.command()
@click.argument("meeting_id")
@click.argument("text")
@click.option("--author", type=click.Choice(["claude", "user"]), default="claude")
def note(meeting_id, text, author):
    """Adiciona uma nota ao vivo ao arquivo da reunião (ação automática, sem aprovação)."""
    meeting_dir = storage.meeting_path(meeting_id)
    notes_path = meeting_dir / "notes.md"
    stamp = datetime.now().strftime("%H:%M")
    tag = "🤖" if author == "claude" else "🧑"
    with notes_path.open("a") as f:
        f.write(f"- `{stamp}` {tag} {text}\n")
    click.echo(json.dumps({"ok": True}))


@cli.group()
def action():
    """Gerencia ações pendentes de aprovação (internas ao vault ou em sistemas externos)."""


@action.command(name="add")
@click.argument("meeting_id")
@click.option("--title", required=True)
@click.option("--detail", default="")
@click.option("--system", default="tolaria", help="Sistema alvo: tolaria, linear, slack, calendar, etc.")
def action_add(meeting_id, title, detail, system):
    """Registra uma ação como pendente. Não executa nada — só sinaliza a intenção."""
    meeting_dir = storage.meeting_path(meeting_id)
    actions_path = meeting_dir / "actions.json"
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
    click.echo(json.dumps(entry, ensure_ascii=False))


@action.command(name="list")
@click.argument("meeting_id")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["pending", "approved", "rejected", "done", "all"]),
    default="all",
)
def action_list(meeting_id, status_filter):
    meeting_dir = storage.meeting_path(meeting_id)
    actions = storage.read_json(meeting_dir / "actions.json", [])
    if status_filter != "all":
        actions = [a for a in actions if a["status"] == status_filter]
    click.echo(json.dumps(actions, ensure_ascii=False, indent=2))


def _set_action_status(meeting_id: str, action_id: str, new_status: str) -> dict:
    meeting_dir = storage.meeting_path(meeting_id)
    actions_path = meeting_dir / "actions.json"
    actions = storage.read_json(actions_path, [])
    for a in actions:
        if a["id"] == action_id:
            a["status"] = new_status
            storage.write_json(actions_path, actions)
            return a
    raise click.ClickException(f"Ação '{action_id}' não encontrada.")


@action.command(name="approve")
@click.argument("meeting_id")
@click.argument("action_id")
def action_approve(meeting_id, action_id):
    """Aprova uma ação pendente. O Claude ainda precisa executá-la e chamar `action done`."""
    click.echo(json.dumps(_set_action_status(meeting_id, action_id, "approved"), ensure_ascii=False))


@action.command(name="reject")
@click.argument("meeting_id")
@click.argument("action_id")
def action_reject(meeting_id, action_id):
    click.echo(json.dumps(_set_action_status(meeting_id, action_id, "rejected"), ensure_ascii=False))


@action.command(name="done")
@click.argument("meeting_id")
@click.argument("action_id")
def action_done(meeting_id, action_id):
    """Marca uma ação aprovada como executada de fato."""
    click.echo(json.dumps(_set_action_status(meeting_id, action_id, "done"), ensure_ascii=False))


@cli.command()
@click.argument("meeting_id")
@click.option(
    "--backend",
    type=click.Choice(list(BATCH_BACKENDS)),
    default=None,
    help="Backend pra transcrição final. Padrão: o mesmo escolhido em `fireball start` (ou whisper).",
)
def finalize(meeting_id, backend):
    """Roda a transcrição final (mais precisa) do áudio completo, por track
    (mic = 'Você', system = 'Outros participantes'), mesclando por ordem de
    início.

    A reconciliação entre a transcrição final e as notas ao vivo (comparar,
    corrigir imprecisões) é feita pelo Claude via skill, não por este comando —
    aqui só produzimos transcript_final.ndjson.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    meeting = storage.read_json(meeting_dir / "meeting.json")
    final_path = meeting_dir / "transcript_final.ndjson"
    if final_path.exists():
        final_path.unlink()

    if meeting.get("fake"):
        for seg in storage.read_ndjson(meeting_dir / "transcript.ndjson"):
            storage.append_ndjson(final_path, {**seg, "source": "final"})
        meeting["status"] = "finalized"
        storage.write_json(meeting_dir / "meeting.json", meeting)
        click.echo(json.dumps({"ok": True, "final_path": str(final_path)}, ensure_ascii=False))
        return

    backend_name = backend or meeting.get("backend") or "whisper"
    language = meeting.get("language") or realtime.DEFAULT_LANGUAGE
    try:
        batch_backend = get_batch_backend(backend_name)
    except BackendUnavailable as exc:
        raise click.ClickException(str(exc))

    tracks = storage.read_json(meeting_dir / "audio_tracks.json", {})
    entries = []
    for key, speaker in (("mic", "Você"), ("system", "Outros participantes")):
        wav_path = meeting_dir / f"{key}.wav"
        if key not in tracks or not wav_path.exists():
            continue
        for seg in batch_backend.transcribe_file(wav_path, language):
            entries.append({"start": seg["start"], "end": seg["end"], "speaker": speaker, "text": seg["text"]})
    # sem timestamp (parakeet hoje), assume início da reunião — não deixa
    # sem posição pra ordenar, só perde a intercalação fina com a outra track
    entries.sort(key=lambda e: e["start"] if e["start"] is not None else 0.0)

    for i, entry in enumerate(entries, start=1):
        storage.append_ndjson(
            final_path,
            {
                "seq": i,
                "start": entry["start"],
                "end": entry["end"],
                "speaker": entry["speaker"],
                "text": entry["text"],
                "source": "final",
            },
        )

    meeting["status"] = "finalized"
    storage.write_json(meeting_dir / "meeting.json", meeting)
    click.echo(json.dumps({"ok": True, "final_path": str(final_path), "segments": len(entries)}, ensure_ascii=False))


if __name__ == "__main__":
    cli()
