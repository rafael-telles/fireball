"""Núcleo do daemon: o dono único do estado do Fireball.

Tudo que muda estado passa por aqui, sob um lock só. Isso é o que torna a
invariante "no máximo uma reunião gravando" de verdade: antes ela era
*derivada* do filesystem (varrer meeting.json procurando status 'recording'),
o que é check-then-act — dois `fireball start` simultâneos passavam os dois
pela verificação e disputavam o microfone. Agora a reunião ativa é um campo
em memória, protegido por lock, num processo único.

E, principalmente: o daemon **supervisiona** os processos filhos. Uma thread
por job faz `proc.wait()` e escreve o status final quando o filho morre. Sem
isso (o desenho anterior), um engine que terminava sozinho ou quebrava
deixava `status: "recording"` gravado para sempre, e o app inteiro ficava
travado — nenhuma reunião nova era aceita até editar o JSON na mão.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from fireball import control, settings, storage
from fireball.daemon import protocol


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: int) -> list[str]:
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _is_engine_of(pid: int, meeting_id: str) -> bool:
    """Confirma que o PID é mesmo um engine desta reunião, e não um PID
    reciclado pelo sistema — o desenho anterior mandava SIGTERM para um
    `engine_pid` lido do JSON sem checar nada, o que podia matar um processo
    alheio depois de reuso de PID."""
    parts = _cmdline(pid)
    return "fireball.cli" in parts and "_engine" in parts and meeting_id in parts


@dataclass
class Job:
    """Um processo filho que o daemon supervisiona (engine ou finalize)."""

    meeting_id: str
    kind: str  # "engine" | "finalize"
    pid: int
    proc: Optional[subprocess.Popen] = None  # None = adotado de um daemon anterior
    stopping: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    exit_code: Optional[int] = None

    @property
    def adopted(self) -> bool:
        return self.proc is None

    def wait(self) -> Optional[int]:
        """Bloqueia até o filho sair. Um processo adotado não é nosso filho,
        então não dá pra `wait()` nele — sobra observar o PID sumir, e o
        código de saída fica desconhecido (None)."""
        if self.proc is not None:
            return self.proc.wait()
        while _pid_alive(self.pid):
            time.sleep(0.5)
        return None

    def signal(self, sig: int) -> None:
        """Manda um sinal ao filho, com o mesmo cuidado do terminate(): um PID
        adotado só é sinalizado depois de confirmado que ainda é o engine
        desta reunião, e não um PID reciclado pelo sistema."""
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.send_signal(sig)
        elif _is_engine_of(self.pid, self.meeting_id):
            os.kill(self.pid, sig)

    def terminate(self) -> None:
        if self.proc is not None:
            # Popen.terminate() já é no-op se o filho foi colhido, o que evita
            # sinalizar um PID reciclado.
            self.proc.terminate()
        elif _is_engine_of(self.pid, self.meeting_id):
            os.kill(self.pid, signal.SIGTERM)


class DaemonCore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._recording: Optional[Job] = None
        self._finalizing: dict[str, Job] = {}
        self._summarizing: dict[str, Job] = {}
        self._started_at = storage.now_iso()
        self._shutting_down = False
        # preenchido pela casca gráfica (fireball.gui.shell) quando ela sobe;
        # continua None num daemon sem sessão gráfica
        self._window_opener: Optional[Callable[[], None]] = None

    # ---------------------------------------------------------------- boot

    def recover(self) -> list[dict]:
        """Reconcilia o que sobrou de uma vida anterior do daemon.

        Se o daemon foi morto sem cerimônia (SIGKILL, queda de sessão), o
        engine — que sobe destacado — vira órfão e continua gravando. Aqui,
        para cada reunião que o disco dá como viva: se o processo ainda
        existe e é mesmo o engine dela, adotamos (voltamos a supervisionar);
        senão, o status vira 'crashed' e a reunião deixa de bloquear o app.
        """
        recovered = []
        with self._lock:
            # reuniões do modelo antigo (duas transcrições) viram uma só antes
            # de qualquer leitura, para ninguém ver a versão pior de uma
            # reunião já finalizada
            migrated = control.migrate_transcripts()
            if migrated:
                print(
                    f"[daemon] transcrição unificada em {len(migrated)} reunião(ões) do formato antigo",
                    flush=True,
                )
            for meeting in control.scan_live_meetings():
                meeting_id = meeting["id"]
                pid = meeting.get("engine_pid")
                if pid and _is_engine_of(pid, meeting_id) and self._recording is None:
                    job = Job(meeting_id=meeting_id, kind="engine", pid=pid)
                    self._recording = job
                    self._supervise(job)
                    # o engine adotado guarda a própria pausa (os parecord
                    # seguem parados); dar 'recording' aqui mostraria uma
                    # reunião gravando que não está capturando nada.
                    status = "paused" if meeting.get("status") == "paused" else "recording"
                    recovered.append(control.update_meeting(meeting_id, status=status))
                else:
                    recovered.append(control.update_meeting(meeting_id, status="crashed"))
        return recovered

    # ----------------------------------------------------------- supervisão

    def _supervise(self, job: Job) -> None:
        threading.Thread(target=self._await_job, args=(job,), daemon=True).start()

    def _await_job(self, job: Job) -> None:
        code = job.wait()
        transcript_changed = False
        with self._lock:
            job.exit_code = code
            if job.kind == "engine":
                # code 0 = saiu limpo por conta própria (roteiro fake acabou);
                # tratamos igual a um stop pedido — a gravação terminou bem.
                status = "stopped" if (job.stopping or code in (0, None)) else "crashed"
                control.update_meeting(job.meeting_id, status=status, exit_code=code)
                transcript_changed = status == "stopped"
                if self._recording is job:
                    self._recording = None
            elif job.kind == "finalize":
                status = "finalized" if code == 0 else "finalize_failed"
                control.update_meeting(job.meeting_id, status=status)
                # a transcrição foi reescrita por cima, melhor: o resumo, o
                # nome e as tags saíram da versão antiga e ficaram velhos
                transcript_changed = status == "finalized"
                if self._finalizing.get(job.meeting_id) is job:
                    del self._finalizing[job.meeting_id]
            else:
                # Resumo não é etapa do ciclo de vida da reunião: uma reunião
                # finalizada continua finalizada se o resumo falhar. Por isso
                # ele tem estado próprio, e não toca em `status`.
                if code == 0:
                    self._apply_summary_metadata(job.meeting_id)
                control.write_meeting_fields(
                    job.meeting_id, summary_status="ok" if code == 0 else "failed"
                )
                if self._summarizing.get(job.meeting_id) is job:
                    del self._summarizing[job.meeting_id]
            job.done.set()

        # fora do lock e depois do `done`: quem esperava a gravação fechar não
        # tem por que esperar também o resumo subir.
        if transcript_changed:
            self._auto_summarize(job.meeting_id)

    def _apply_summary_metadata(self, meeting_id: str) -> None:
        """Leva o nome e as tags do resumo para o meeting.json.

        Falhar aqui não pode derrubar a thread de supervisão nem transformar um
        resumo que ficou pronto em 'failed': o resumo está no disco, e o que se
        perde é a etiqueta.
        """
        try:
            control.apply_summary_metadata(meeting_id)
        except Exception as exc:  # noqa: BLE001 — supervisão não morre por isto
            print(f"[daemon] nome/tags de {meeting_id} não aplicados: {exc}", flush=True)

    def _auto_summarize(self, meeting_id: str) -> None:
        """Sobe o resumo sozinho quando a reunião ganha uma transcrição nova.

        É o que faz a reunião sem nome ganhar um: nomear só no clique de
        *Gerar* deixaria o histórico cheio de "Sem nome" até alguém lembrar de
        pedir, e ninguém lembra.

        Duas recusas de propósito. Reunião `fake` não gera: o motor simulado
        existe para exercitar o pipeline **sem chave de API**, e um roteiro de
        teste virando chamada paga contraria justamente isso. E reunião sem
        fala nenhuma também não: o job só falharia, deixando um erro na tela
        que não é sobre nada.
        """
        try:
            if not settings.load()["auto_summarize"]:
                return
            if control.read_meeting(meeting_id).get("fake"):
                return
            if not control.read_transcript(meeting_id):
                return
            self.summarize(meeting_id)
        except control.MeetingBusy:
            pass  # já tem um resumo rodando; o que ele escrever serve igual
        except Exception as exc:  # noqa: BLE001 — supervisão não morre por isto
            print(f"[daemon] resumo automático de {meeting_id} não subiu: {exc}", flush=True)

    def _spawn(self, meeting_id: str, kind: str, cmd: list[str], log_name: str) -> Job:
        meeting_dir = storage.meeting_path(meeting_id)
        log_file = open(meeting_dir / log_name, "w")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()  # o filho herdou o fd; o nosso não serve pra mais nada
        return Job(meeting_id=meeting_id, kind=kind, pid=proc.pid, proc=proc)

    # ---------------------------------------------------------------- ops

    def set_window_opener(self, opener: Optional[Callable[[], None]]) -> None:
        """A casca gráfica registra aqui como trazer a janela à frente.

        Pode ser chamado de qualquer thread do servidor, então a casca é quem
        se vira pra cruzar a fronteira até o thread do Qt com segurança (ver
        `fireball.gui.shell.Bridge`)."""
        with self._lock:
            self._window_opener = opener

    def has_shell(self) -> bool:
        with self._lock:
            return self._window_opener is not None

    def show_window(self) -> dict:
        with self._lock:
            opener = self._window_opener
        if opener is None:
            raise RuntimeError(
                "Este daemon está rodando sem casca gráfica (sem sessão gráfica ou sem o extra [gui]); "
                "não há janela nem ícone de bandeja pra mostrar."
            )
        opener()
        return {"ok": True}

    def ping(self) -> dict:
        return {"pong": True, "pid": os.getpid(), "protocol": protocol.PROTOCOL_VERSION}

    def daemon_status(self) -> dict:
        with self._lock:
            return {
                "pid": os.getpid(),
                "socket": str(protocol.socket_path()),
                "started_at": self._started_at,
                "protocol": protocol.PROTOCOL_VERSION,
                "shell": self._window_opener is not None,
                "active": control.read_meeting(self._recording.meeting_id) if self._recording else None,
                "finalizing": sorted(self._finalizing),
            }

    def active(self) -> Optional[dict]:
        with self._lock:
            if self._recording is None:
                return None
            return control.read_meeting(self._recording.meeting_id)

    def start_meeting(
        self,
        name: Optional[str] = None,
        fake: bool = True,
        interval: float = 3.0,
        mic_device: Optional[str] = None,
        system_device: Optional[str] = None,
        transcribe: Optional[bool] = None,
        backend: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict:
        """Nada de backend/idioma obrigatórios: o que o chamador não disser sai
        da configuração (ver `fireball.settings`). É o que deixa a janela
        perguntar só nome e modo — o resto foi decidido uma vez, na tela de
        configuração, e vale igual pra CLI.

        Nome também não é obrigatório: reunião nasce sem nome e o provedor de
        resumo escreve um depois, a partir do que foi dito (ver
        `_auto_summarize`)."""
        prefs = settings.load()
        transcribe = prefs["transcribe_live"] if transcribe is None else transcribe
        backend = backend or prefs["realtime_backend"]
        language = language or prefs["language"]

        with self._lock:
            if self._shutting_down:
                raise control.MeetingBusy("O daemon está desligando; não dá pra iniciar reunião agora.")
            if self._recording is not None:
                current = control.read_meeting(self._recording.meeting_id)
                raise control.MeetingAlreadyActive(
                    f"Já tem uma reunião em andamento: '{current['name']}' ({current['id']}). "
                    "Pare ela antes de iniciar outra."
                )

            meeting = control.create_meeting(
                name=name,
                fake=fake,
                mic_device=mic_device,
                system_device=system_device,
                transcribe=transcribe,
                backend=backend,
                language=language,
            )
            meeting_id = meeting["id"]
            cmd = control.engine_command(
                meeting_id,
                fake=fake,
                interval=interval,
                mic_device=mic_device,
                system_device=system_device,
                transcribe=transcribe,
                backend=backend,
                language=language,
            )
            try:
                job = self._spawn(meeting_id, "engine", cmd, "engine.log")
            except OSError as exc:
                control.update_meeting(meeting_id, status="failed")
                raise RuntimeError(f"Não consegui subir o engine da reunião: {exc}") from exc

            self._recording = job
            self._supervise(job)
            return control.update_meeting(meeting_id, status="recording", engine_pid=job.pid)

    def stop_meeting(self, meeting_id: Optional[str] = None, wait_timeout: float = 0.0) -> dict:
        """Pede SIGTERM ao engine e marca 'stopping'. O status só vira
        'stopped' quando o filho realmente sai — o engine ainda empacota os
        .wav e drena a transcrição depois do sinal, e marcar 'stopped' antes
        disso (o que o desenho anterior fazia) deixava um `finalize` logo em
        seguida sem `mic.wav` pra ler.
        """
        with self._lock:
            job = self._recording
            if job is None:
                if meeting_id is None:
                    raise control.NoActiveMeeting("Não há reunião gravando.")
                return control.read_meeting(meeting_id)
            if meeting_id is not None and job.meeting_id != meeting_id:
                return control.read_meeting(meeting_id)
            if not job.stopping:
                job.stopping = True
                control.update_meeting(job.meeting_id, status="stopping")
                job.terminate()

        if wait_timeout:
            job.done.wait(timeout=wait_timeout)
        return control.read_meeting(job.meeting_id)

    def pause_meeting(self, meeting_id: Optional[str] = None) -> dict:
        """Congela a captura sem encerrar a reunião."""
        return self._set_paused(meeting_id, True)

    def resume_meeting(self, meeting_id: Optional[str] = None) -> dict:
        return self._set_paused(meeting_id, False)

    def _set_paused(self, meeting_id: Optional[str], paused: bool) -> dict:
        with self._lock:
            job = self._recording
            if job is None:
                raise control.NoActiveMeeting("Não há reunião gravando agora.")
            if meeting_id and meeting_id != job.meeting_id:
                raise control.MeetingBusy(
                    f"A reunião gravando agora é '{job.meeting_id}', não '{meeting_id}'."
                )

            current = control.read_meeting(job.meeting_id)
            if current.get("status") == "stopping":
                raise control.MeetingBusy("A reunião já está parando.")
            if (current.get("status") == "paused") == paused:
                return current  # já está como se pediu; repetir não é erro

            job.signal(signal.SIGUSR1 if paused else signal.SIGUSR2)
            return control.update_meeting(job.meeting_id, status="paused" if paused else "recording")

    def delete_meeting(self, meeting_id: str) -> dict:
        """Apaga a reunião e tudo que ela gravou.

        Recusa enquanto há processo mexendo nela — gravando, finalizando ou
        resumindo. Apagar a pasta debaixo de um processo que está escrevendo
        nela deixaria arquivo órfão e o job falharia sem explicação.
        """
        with self._lock:
            meeting = control.read_meeting(meeting_id)
            status = meeting.get("status")
            if status in control.LIVE_STATUSES:
                raise control.MeetingBusy(
                    f"A reunião '{meeting_id}' está {status}; pare ela antes de excluir."
                )
            if meeting_id in self._finalizing:
                raise control.MeetingBusy(f"A reunião '{meeting_id}' está sendo finalizada.")
            if meeting_id in self._summarizing:
                raise control.MeetingBusy(f"O resumo da reunião '{meeting_id}' está sendo gerado.")
            return control.delete_meeting(meeting_id)

    def transcript_ack(self, meeting_id: str, seq: int) -> dict:
        with self._lock:
            storage.write_json(storage.meeting_path(meeting_id) / "checkpoint.json", {"last_seq": seq})
        return {"last_seq": seq}

    def meeting_status(self, meeting_id: str) -> dict:
        return control.get_meeting_status(meeting_id)

    def list_meetings(self) -> list[dict]:
        return control.list_meetings()

    def get_settings(self) -> dict:
        return settings.load()

    def update_settings(self, **fields) -> dict:
        with self._lock:
            return settings.save(**fields)

    def meeting_summaries(self) -> list[dict]:
        return control.meeting_summaries()

    def transcript(self, meeting_id: str, since_seq: int = 0) -> list[dict]:
        return control.read_transcript(meeting_id, since_seq=since_seq)

    def _editable(self, meeting_id: str) -> None:
        """Recusa mexer na transcrição de uma reunião viva.

        O engine ainda está dando append no mesmo arquivo; reescrevê-lo por
        baixo dele perderia as falas que chegassem entre a leitura e a troca.
        """
        if control.read_meeting(meeting_id).get("status") in control.LIVE_STATUSES:
            raise control.MeetingBusy(
                "Dá pra mexer na transcrição depois que a reunião terminar — "
                "enquanto ela grava, o motor ainda está escrevendo neste arquivo."
            )

    def edit_segment(self, meeting_id: str, seq: int, text: str) -> dict:
        with self._lock:
            self._editable(meeting_id)
            return control.edit_segment(meeting_id, seq, text)

    def delete_segment(self, meeting_id: str, seq: int) -> dict:
        with self._lock:
            self._editable(meeting_id)
            return control.delete_segment(meeting_id, seq)

    def warnings(self, meeting_id: str) -> list[dict]:
        return control.read_warnings(meeting_id)

    def audio_info(self, meeting_id: str) -> dict:
        return control.audio_info(meeting_id)

    def summary(self, meeting_id: str) -> dict:
        """O resumo e o estado de quem o gera.

        Os três vão juntos porque a tela precisa dos três para dizer a verdade:
        ter markdown não significa que a geração de agora terminou, e não ter
        pode ser "nunca gerou" ou "acabou de falhar" — que pedem telas
        diferentes.
        """
        meeting = control.read_meeting(meeting_id)
        return {
            "summary": control.read_summary(meeting_id),
            "status": meeting.get("summary_status"),
            "provider": meeting.get("summary_provider"),
            "error": self._summary_error(meeting_id),
        }

    def summarize(self, meeting_id: str, provider: Optional[str] = None, wait_timeout: float = 0.0) -> dict:
        """Gera (ou regera) o resumo, como job supervisionado.

        Mesma forma do finalize — processo separado, log próprio, status que a
        janela acompanha pelo polling — porque a chamada ao provedor demora e
        pode falhar de fora (sem login, sem rede), e nada disso pode parar o
        daemon.
        """
        provider = provider or settings.load()["summary_provider"]
        with self._lock:
            control.read_meeting(meeting_id)  # 404 cedo, antes de subir processo
            if meeting_id in self._summarizing:
                raise control.MeetingBusy(f"O resumo da reunião '{meeting_id}' já está sendo gerado.")

            # o resumo anterior sai de cena junto com o pedido de regerar: se
            # este falhar, mostrar o antigo como se fosse o novo seria mentira.
            meeting_dir = storage.meeting_path(meeting_id)
            (meeting_dir / "summary.md").unlink(missing_ok=True)
            (meeting_dir / "summary_result.json").unlink(missing_ok=True)

            cmd = control.summarize_command(meeting_id, provider)
            job = self._spawn(meeting_id, "summary", cmd, "summary.log")
            self._summarizing[meeting_id] = job
            self._supervise(job)
            control.write_meeting_fields(meeting_id, summary_status="running", summary_provider=provider)

        if wait_timeout:
            job.done.wait(timeout=wait_timeout)

        return {
            "meeting_id": meeting_id,
            "provider": provider,
            "status": control.read_meeting(meeting_id).get("summary_status"),
            "summary": control.read_summary(meeting_id),
            "error": self._summary_error(meeting_id),
        }

    def _summary_error(self, meeting_id: str) -> Optional[str]:
        """A última linha útil do summary.log, quando o job falhou.

        O provedor já levanta mensagens escritas para gente ler ("Not logged
        in", "comando 'claude' não encontrado"); jogá-las na tela é melhor que
        um "falhou" que obriga a caçar log.
        """
        meeting = control.read_meeting(meeting_id)
        if meeting.get("summary_status") != "failed":
            return None
        log = storage.meeting_path(meeting_id) / "summary.log"
        if not log.exists():
            return None
        lines = [line.strip() for line in log.read_text().splitlines() if line.strip()]
        if not lines:
            return None
        # a última linha do traceback vem como "pacote.Excecao: mensagem"; a
        # mensagem já foi escrita para alguém ler, o nome da classe não.
        last = lines[-1]
        head, sep, rest = last.partition(": ")
        return rest if sep and "." in head and " " not in head else last

    def notes(self, meeting_id: str) -> str:
        return control.read_notes(meeting_id)

    def save_notes(self, meeting_id: str, text: str) -> dict:
        with self._lock:
            control.write_notes(meeting_id, text)
        return {"saved": True}

    def rename_meeting(self, meeting_id: str, name: str) -> dict:
        with self._lock:
            return control.rename_meeting(meeting_id, name)

    def note(self, meeting_id: str, text: str, author: str = "claude") -> dict:
        with self._lock:
            control.append_note(meeting_id, text, author, datetime.now().strftime("%H:%M"))
        return {"ok": True}

    def finalize(self, meeting_id: str, backend: Optional[str] = None, wait_timeout: float = 0.0) -> dict:
        # sem backend explícito vale o configurado para a transcrição final,
        # que não é necessariamente o do tempo real (é comum querer 'groq'
        # aqui e um motor local durante a reunião).
        backend = backend or settings.load()["final_backend"]
        with self._lock:
            meeting = control.read_meeting(meeting_id)
            if meeting.get("status") in control.LIVE_STATUSES:
                raise control.MeetingBusy(
                    f"A reunião '{meeting_id}' ainda está {meeting['status']}; pare ela antes de finalizar."
                )
            if meeting_id in self._finalizing:
                raise control.MeetingBusy(f"A reunião '{meeting_id}' já está sendo finalizada.")

            # limpa o resumo da finalização anterior: se esta falhar, não
            # podemos devolver os números da tentativa passada como se fossem
            # desta.
            (storage.meeting_path(meeting_id) / "finalize_result.json").unlink(missing_ok=True)

            cmd = control.finalize_command(meeting_id, backend)
            job = self._spawn(meeting_id, "finalize", cmd, "finalize.log")
            self._finalizing[meeting_id] = job
            self._supervise(job)
            control.update_meeting(meeting_id, status="finalizing")

        if wait_timeout:
            job.done.wait(timeout=wait_timeout)

        meeting_dir = storage.meeting_path(meeting_id)
        result = storage.read_json(meeting_dir / "finalize_result.json", {})
        return {
            "meeting_id": meeting_id,
            "status": control.read_meeting(meeting_id)["status"],
            "exit_code": job.exit_code,
            **result,
        }

    def shutdown(self) -> dict:
        """Desligar o daemon para também a reunião ativa.

        Enquanto o Fireball está ligado pode haver no máximo uma gravação; ao
        desligar, ela para junto e fecha os arquivos direito — nada de engine
        órfão gravando com ninguém olhando.
        """
        with self._lock:
            self._shutting_down = True
            job = self._recording
        if job is not None:
            self.stop_meeting(job.meeting_id, wait_timeout=90.0)
        return {"ok": True}
