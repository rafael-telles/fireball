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
from typing import Optional

from fireball import control, realtime, storage
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
        self._started_at = storage.now_iso()
        self._shutting_down = False

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
            for meeting in control.scan_live_meetings():
                meeting_id = meeting["id"]
                pid = meeting.get("engine_pid")
                if pid and _is_engine_of(pid, meeting_id) and self._recording is None:
                    job = Job(meeting_id=meeting_id, kind="engine", pid=pid)
                    self._recording = job
                    self._supervise(job)
                    recovered.append(control.update_meeting(meeting_id, status="recording"))
                else:
                    recovered.append(control.update_meeting(meeting_id, status="crashed"))
        return recovered

    # ----------------------------------------------------------- supervisão

    def _supervise(self, job: Job) -> None:
        threading.Thread(target=self._await_job, args=(job,), daemon=True).start()

    def _await_job(self, job: Job) -> None:
        code = job.wait()
        with self._lock:
            job.exit_code = code
            if job.kind == "engine":
                # code 0 = saiu limpo por conta própria (roteiro fake acabou);
                # tratamos igual a um stop pedido — a gravação terminou bem.
                status = "stopped" if (job.stopping or code in (0, None)) else "crashed"
                control.update_meeting(job.meeting_id, status=status, exit_code=code)
                if self._recording is job:
                    self._recording = None
            else:
                status = "finalized" if code == 0 else "finalize_failed"
                control.update_meeting(job.meeting_id, status=status)
                if self._finalizing.get(job.meeting_id) is job:
                    del self._finalizing[job.meeting_id]
            job.done.set()

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

    def ping(self) -> dict:
        return {"pong": True, "pid": os.getpid(), "protocol": protocol.PROTOCOL_VERSION}

    def daemon_status(self) -> dict:
        with self._lock:
            return {
                "pid": os.getpid(),
                "socket": str(protocol.socket_path()),
                "started_at": self._started_at,
                "protocol": protocol.PROTOCOL_VERSION,
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
        name: str = "Reunião",
        fake: bool = True,
        interval: float = 3.0,
        mic_device: Optional[str] = None,
        system_device: Optional[str] = None,
        transcribe: bool = True,
        backend: str = "whisper",
        language: str = realtime.DEFAULT_LANGUAGE,
    ) -> dict:
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

    def transcript_ack(self, meeting_id: str, seq: int) -> dict:
        with self._lock:
            storage.write_json(storage.meeting_path(meeting_id) / "checkpoint.json", {"last_seq": seq})
        return {"last_seq": seq}

    def meeting_status(self, meeting_id: str) -> dict:
        return control.get_meeting_status(meeting_id)

    def list_meetings(self) -> list[dict]:
        return control.list_meetings()

    def note(self, meeting_id: str, text: str, author: str = "claude") -> dict:
        with self._lock:
            control.append_note(meeting_id, text, author, datetime.now().strftime("%H:%M"))
        return {"ok": True}

    def action_add(self, meeting_id: str, title: str, detail: str = "", system: str = "tolaria") -> dict:
        with self._lock:
            return control.add_action(meeting_id, title, detail, system)

    def action_list(self, meeting_id: str, status_filter: str = "all") -> list[dict]:
        return control.list_actions(meeting_id, status_filter)

    def action_set(self, meeting_id: str, action_id: str, status: str) -> dict:
        with self._lock:
            return control.set_action_status(meeting_id, action_id, status)

    def finalize(self, meeting_id: str, backend: Optional[str] = None, wait_timeout: float = 0.0) -> dict:
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
