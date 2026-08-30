"""CLI do Fireball.

Este CLI é a tool que o Claude usa para agir como escrivão de uma reunião:
iniciar/parar gravação, acompanhar a transcrição ao vivo, tomar notas e
gerenciar ações pendentes de aprovação. Ver skills/fireball/SKILL.md para
o fluxo completo que o Claude segue.

**A CLI é um cliente do daemon**, não uma implementação paralela: todo
comando que muda estado vira uma chamada ao daemon (`fireball.daemon`), que é
o dono único da reunião ativa e dos processos de gravação. O daemon sobe
sozinho no primeiro comando que precisar dele.

A única exceção é a *leitura* da transcrição (`transcript show/follow`), que
lê `transcript.ndjson` direto do disco: é um stream contínuo, e passá-lo pelo
socket não daria nenhuma garantia a mais — o arquivo é append-only e tem um
escritor só (o engine).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

# Carrega segredos (ex.: GROQ_API_KEY) do .env na raiz do repo, se existir.
# Roda pra qualquer subcomando, incluindo o `_engine`/`_finalize` em background
# (que reimportam este módulo via `python -m fireball.cli`).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fireball import audio, control, engine, finalize as finalize_mod, realtime, storage, summary as summary_mod
from fireball.backends import BATCH_BACKENDS, REALTIME_BACKENDS, BackendUnavailable
from fireball.summarizers import SUMMARY_PROVIDERS
from fireball.daemon import client, protocol, server as daemon_server


def _echo(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False))


def _call(op: str, autostart: bool = True, **args):
    """Chama o daemon traduzindo as falhas dele em erro de CLI legível.

    `autostart=False` para comandos cujo trabalho é justamente informar se o
    daemon está de pé — senão consultar o estado subiria um daemon novo logo
    depois de alguém desligá-lo de propósito.
    """
    try:
        return client.call(op, autostart=autostart, **args)
    except protocol.DaemonUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    except protocol.DaemonError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def cli():
    """Fireball — grava e transcreve reuniões, servindo de tools para o Claude atuar como escrivão."""


# --------------------------------------------------------------------- daemon


@cli.group()
def daemon():
    """Controla o daemon — o processo que é dono do estado das reuniões."""


@daemon.command(name="run")
@click.option(
    "--tray/--no-tray",
    default=True,
    help="Mostra o ícone na bandeja (padrão). Com --no-tray o daemon roda sem interface.",
)
def daemon_run(tray):
    """Sobe o daemon em foreground (útil pra depurar; o log vai pro terminal)."""
    try:
        sys.exit(daemon_server.run(tray=tray))
    except daemon_server.AlreadyRunning as exc:
        raise click.ClickException(str(exc))


@daemon.command(name="start")
@click.option("--tray/--no-tray", default=True, help="Mostra o ícone na bandeja (padrão).")
def daemon_start(tray):
    """Sobe o daemon em background, se já não estiver rodando.

    Com sessão gráfica, subir o daemon faz o ícone aparecer na bandeja — é o
    mesmo processo.
    """
    try:
        client.ensure_daemon(tray=tray)
    except protocol.DaemonUnavailable as exc:
        raise click.ClickException(str(exc))
    _echo(_call("daemon_status", autostart=False))


@daemon.command(name="stop")
def daemon_stop():
    """Desliga o daemon. A reunião ativa, se houver, é parada junto e tem os
    arquivos fechados direito — nada fica gravando órfão."""
    result = client.shutdown()
    _echo(result or {"ok": True, "note": "daemon já não estava rodando"})


@daemon.command(name="status")
def daemon_status():
    """Mostra se o daemon está de pé, onde ele escuta e o que está rodando."""
    if not client.is_running():
        _echo({"running": False, "socket": str(protocol.socket_path()), "log": str(protocol.log_path())})
        return
    try:
        _echo({"running": True, **_call("daemon_status", autostart=False)})
    except click.ClickException:
        # desligou entre o is_running() e a chamada — é "parado", não erro
        _echo({"running": False, "socket": str(protocol.socket_path()), "log": str(protocol.log_path())})


# ------------------------------------------------------------------ reuniões


@cli.command()
def devices():
    """Lista as fontes de áudio disponíveis (mics e monitores de saída), via pactl.

    Use o campo "name" com --mic-device/--system-device em `fireball start`
    quando o padrão (@DEFAULT_SOURCE@ / @DEFAULT_MONITOR@) não for o que você
    quer — por exemplo, um headset bluetooth específico.
    """
    for source in audio.list_sources():
        _echo(source)


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
    default=None,
    help="No modo --real, roda transcrição em tempo real local em paralelo à gravação. Padrão: o configurado.",
)
@click.option(
    "--backend",
    type=click.Choice(list(REALTIME_BACKENDS)),
    default=None,
    help="Motor de transcrição ao vivo: 'whisper' (faster-whisper, multi-idioma) ou 'parakeet' (NeMo Parakeet TDT via ONNX). Padrão: o configurado.",
)
@click.option("--language", default=None, help="Idioma esperado da fala (código curto, ex: pt). Padrão: o configurado.")
def start(name, fake, interval, mic_device, system_device, transcribe, backend, language):
    """Inicia uma nova reunião. O daemon cria a pasta e sobe o engine de
    gravação/transcrição, que ele mesmo supervisiona. Falha se já houver uma
    reunião gravando — só uma por vez.

    Backend, idioma e transcrever-ao-vivo não passados aqui saem da
    configuração (`~/.fireball/settings.json`, editável pela tela de
    configuração da janela) — as flags são o override pontual dela.
    """
    meeting = _call(
        "start",
        name=name,
        fake=fake,
        interval=interval,
        mic_device=mic_device,
        system_device=system_device,
        transcribe=transcribe,
        backend=backend,
        language=language,
    )
    _echo({"meeting_id": meeting["id"], "path": str(storage.meeting_path(meeting["id"]))})


@cli.command()
@click.argument("meeting_id", required=False)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Espera o engine terminar de fechar os arquivos (.wav, última transcrição) antes de retornar.",
)
def stop(meeting_id, wait):
    """Para a reunião ativa (ou a indicada por MEETING_ID).

    Com --wait (padrão), só retorna quando o engine realmente saiu: ele ainda
    empacota os .wav e drena a transcrição depois do sinal, e `finalize` antes
    disso não encontraria o áudio.
    """
    _echo(_call("stop", meeting_id=meeting_id, wait_timeout=90.0 if wait else 0.0, timeout=120.0))


@cli.command()
@click.argument("meeting_id", required=False)
def status(meeting_id):
    """Estado de uma reunião (ou da ativa): metadados, checkpoint, segmentos, ações pendentes."""
    if meeting_id is None:
        active = _call("active")
        if active is None:
            raise click.ClickException("Não há reunião ativa. Passe um MEETING_ID ou rode `fireball list`.")
        meeting_id = active["id"]
    click.echo(json.dumps(_call("meeting_status", meeting_id=meeting_id), ensure_ascii=False, indent=2))


@cli.command(name="list")
def list_meetings():
    """Lista todas as reuniões conhecidas."""
    click.echo(json.dumps(_call("list"), ensure_ascii=False, indent=2))


@cli.command()
def active():
    """Mostra a reunião que está gravando agora, ou null."""
    _echo(_call("active"))


# --------------------------------------------------------------- transcrição


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
        _echo(seg)


@transcript.command(name="follow")
@click.argument("meeting_id")
@click.option("--since", default=None, type=int, help="Retoma a partir deste seq (padrão: usa o checkpoint salvo).")
@click.option("--poll", default=0.5, type=float, help="Intervalo de polling do arquivo, em segundos.")
def transcript_follow(meeting_id, since, poll):
    """Acompanha a transcrição em tempo real, uma linha JSON por segmento novo.

    Pensado para rodar via Bash em background e ser observado com o Monitor tool:
    cada linha de stdout é um segmento novo da reunião. Termina sozinho quando a
    reunião sai de um estado vivo (parada, finalizada ou quebrada) e não há mais
    segmentos novos.
    """
    meeting_dir = storage.meeting_path(meeting_id)
    transcript_path = meeting_dir / "transcript.ndjson"
    checkpoint_path = meeting_dir / "checkpoint.json"

    last_seq = since if since is not None else storage.read_json(checkpoint_path, {"last_seq": 0})["last_seq"]

    while True:
        meeting = storage.read_json(meeting_dir / "meeting.json", {})
        new_any = False
        for seg in storage.read_ndjson(transcript_path, since_seq=last_seq):
            _echo(seg)
            sys.stdout.flush()
            last_seq = seg["seq"]
            new_any = True
        if meeting.get("status") not in control.LIVE_STATUSES and not new_any:
            break
        time.sleep(poll)


@transcript.command(name="ack")
@click.argument("meeting_id")
@click.argument("seq", type=int)
def transcript_ack(meeting_id, seq):
    """Marca até onde o Claude já processou o stream (para retomar após reconexão)."""
    _echo(_call("transcript_ack", meeting_id=meeting_id, seq=seq))


# ---------------------------------------------------------------- notas/ações


@cli.command()
@click.argument("meeting_id")
@click.argument("text")
@click.option("--author", type=click.Choice(["claude", "user"]), default="claude")
def note(meeting_id, text, author):
    """Adiciona uma nota ao vivo ao arquivo da reunião (ação automática, sem aprovação)."""
    _echo(_call("note", meeting_id=meeting_id, text=text, author=author))


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
    _echo(_call("action_add", meeting_id=meeting_id, title=title, detail=detail, system=system))


@action.command(name="list")
@click.argument("meeting_id")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["pending", "approved", "rejected", "done", "all"]),
    default="all",
)
def action_list(meeting_id, status_filter):
    click.echo(
        json.dumps(_call("action_list", meeting_id=meeting_id, status_filter=status_filter), ensure_ascii=False, indent=2)
    )


@action.command(name="approve")
@click.argument("meeting_id")
@click.argument("action_id")
def action_approve(meeting_id, action_id):
    """Aprova uma ação pendente. O Claude ainda precisa executá-la e chamar `action done`."""
    _echo(_call("action_set", meeting_id=meeting_id, action_id=action_id, status="approved"))


@action.command(name="reject")
@click.argument("meeting_id")
@click.argument("action_id")
def action_reject(meeting_id, action_id):
    _echo(_call("action_set", meeting_id=meeting_id, action_id=action_id, status="rejected"))


@action.command(name="done")
@click.argument("meeting_id")
@click.argument("action_id")
def action_done(meeting_id, action_id):
    """Marca uma ação aprovada como executada de fato."""
    _echo(_call("action_set", meeting_id=meeting_id, action_id=action_id, status="done"))


# ------------------------------------------------------------------ finalize


@cli.command()
@click.argument("meeting_id")
@click.option(
    "--backend",
    type=click.Choice(list(BATCH_BACKENDS)),
    default=None,
    help="Backend pra transcrição final. Padrão: o configurado para a transcrição final.",
)
@click.option("--wait/--no-wait", default=True, help="Espera a transcrição final terminar.")
@click.option("--timeout", default=3600.0, type=float, help="Tempo máximo de espera, em segundos.")
def finalize(meeting_id, backend, wait, timeout):
    """Roda a transcrição final (mais precisa) do áudio completo, por track
    (mic = 'Você', system = 'Outros participantes'), mesclando por ordem de
    início.

    O daemon roda isso num processo separado e supervisiona: o status vai pra
    'finalizing' e depois 'finalized' (ou 'finalize_failed'). A reconciliação
    entre a transcrição final e as notas ao vivo é feita pelo Claude via
    skill, não por este comando.
    """
    result = _call(
        "finalize",
        meeting_id=meeting_id,
        backend=backend,
        wait_timeout=timeout if wait else 0.0,
        timeout=timeout + 30.0,
    )
    _echo(result)
    if wait and result.get("status") != "finalized":
        raise click.ClickException(
            f"Finalização não concluiu (status: {result.get('status')}). "
            f"Veja {storage.meeting_path(meeting_id) / 'finalize.log'}."
        )


# ------------------------------------------- processos internos (do daemon)


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
    """Processo de gravação. Subido e supervisionado pelo daemon — não chame na mão."""
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
@click.option("--meeting-id", default=None, help="Por padrão, a reunião que estiver gravando.")
def pause(meeting_id):
    """Pausa a captura sem encerrar a reunião.

    O engine continua de pé e o microfone continua tomado por ela — o que não
    for capturado simplesmente não entra na gravação, então a transcrição
    continua alinhada com o áudio.
    """
    _echo(_call("pause", meeting_id=meeting_id))


@cli.command()
@click.option("--meeting-id", default=None, help="Por padrão, a reunião que estiver pausada.")
def resume(meeting_id):
    """Retoma uma reunião pausada."""
    _echo(_call("resume", meeting_id=meeting_id))


@cli.command()
@click.argument("meeting_id")
@click.option("--yes", is_flag=True, help="Não perguntar.")
def delete(meeting_id, yes):
    """Apaga a reunião e tudo que ela gravou — áudio, transcrições e notas.

    Não tem desfazer: não existe lixeira.
    """
    if not yes:
        click.confirm(
            f"Apagar '{meeting_id}' e todo o áudio, transcrição e notas dela? Isso não tem volta.",
            abort=True,
        )
    _echo(_call("delete", meeting_id=meeting_id))


@cli.command()
@click.argument("meeting_id")
@click.option(
    "--provider",
    type=click.Choice(list(SUMMARY_PROVIDERS)),
    default=None,
    help="Quem escreve o resumo. Padrão: o configurado.",
)
@click.option("--wait/--no-wait", default=True, help="Espera o resumo ficar pronto.")
@click.option("--timeout", default=900.0, type=float, help="Tempo máximo de espera, em segundos.")
def summarize(meeting_id, provider, wait, timeout):
    """Resume a reunião a partir da transcrição (a final, se houver).

    Regera por cima do resumo anterior — o resumo é derivado da transcrição,
    não um registro histórico.
    """
    _echo(_call("summarize", meeting_id=meeting_id, provider=provider, wait_timeout=timeout if wait else 0.0))


@cli.command(name="_summarize", hidden=True)
@click.argument("meeting_id")
@click.option("--provider", default="claude_code")
def _summarize_cmd(meeting_id, provider):
    """Processo de resumo. Subido e supervisionado pelo daemon — não chame na mão."""
    _echo(summary_mod.run_summary(storage.meeting_path(meeting_id), provider))


@cli.command(name="_finalize", hidden=True)
@click.argument("meeting_id")
@click.option("--backend", default=None)
def _finalize_cmd(meeting_id, backend):
    """Processo de transcrição final. Subido e supervisionado pelo daemon —
    não escreve status, quem faz isso é o daemon ao ver este processo sair."""
    try:
        finalize_mod.run_finalize(storage.meeting_path(meeting_id), backend)
    except BackendUnavailable as exc:
        raise click.ClickException(str(exc))


if __name__ == "__main__":
    cli()
