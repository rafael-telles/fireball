"""Ponte entre o JavaScript da janela e o núcleo do daemon.

A janela é renderizada **pelo próprio daemon** (ver `fireball.gui.shell`), então
aqui chamamos o `DaemonCore` direto, em processo — não faria sentido o daemon
abrir um socket para falar consigo mesmo. Continua havendo uma autoridade só:
todo método passa pelo mesmo core, sob o mesmo lock que serializa a CLI.

O envelope `{ok, result}` / `{ok, error}` é o mesmo que o socket devolve, para
o front-end (`web/app.js`) não precisar saber por onde a chamada veio.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fireball.backends import BATCH_BACKENDS, REALTIME_BACKENDS
from fireball.calendars import CALENDAR_PROVIDERS
from fireball.summarizers import SUMMARY_PROVIDERS
from fireball.daemon.core import DaemonCore


class Api:
    def __init__(self, core: DaemonCore, audio_base_url: str = ""):
        self._core = core
        # de onde a janela puxa o áudio das reuniões (ver gui/audio_server.py);
        # vazio quando não há servidor, e aí o player some em vez de quebrar
        self._audio_base_url = audio_base_url

    def _call(self, fn, **args) -> dict:
        try:
            return {"ok": True, "result": fn(**args)}
        except Exception as exc:  # noqa: BLE001 — vira mensagem na tela, não crash
            return {"ok": False, "error": str(exc), "kind": type(exc).__name__}

    def get_status(self) -> dict:
        result = self._call(self._core.daemon_status)
        if not result["ok"]:
            return result
        active = result["result"]["active"]
        if not active:
            return {"ok": True, "result": {"active": None}}
        detail = self._call(self._core.meeting_status, meeting_id=active["id"])
        return {"ok": True, "result": {"active": detail["result"]}} if detail["ok"] else detail

    def backend_options(self) -> dict:
        """O que a tela de configuração pode oferecer em cada modo. 'groq' só
        aparece no final: é API paga por requisição, e no tempo real viraria
        uma chamada a cada poucos segundos."""
        return {
            "realtime": list(REALTIME_BACKENDS),
            "final": list(BATCH_BACKENDS),
            "summary": list(SUMMARY_PROVIDERS),
            "calendar": list(CALENDAR_PROVIDERS),
        }

    def get_settings(self) -> dict:
        return self._call(self._core.get_settings)

    def save_settings(self, values: dict) -> dict:
        return self._call(self._core.update_settings, **values)

    def agenda(self, refresh: bool = False) -> dict:
        """Próximos eventos. `refresh=True` força um fetch novo no provedor."""
        return self._call(self._core.agenda, refresh=bool(refresh))

    def start_meeting(self, name: str = "", event_id: str = "", diarize: bool | None = None) -> dict:
        """Começar reunião pela janela é um clique: nem o nome é obrigatório.

        Nome vazio é o caminho normal, não um campo esquecido — a reunião nasce
        sem nome e o provedor de resumo escreve um quando ela acaba, junto com
        as tags. Quem já sabe o nome digita, e aí a IA não mexe.

        `event_id` inicia a partir de um evento da agenda: o daemon resolve o
        evento no cache e grava nome/local/descrição/convidados no meeting.

        `fake=False` fixo — o motor simulado existe pra testar o pipeline sem
        microfone, o que é trabalho de desenvolvimento (`fireball start
        --fake`), não escolha de quem abriu a janela pra gravar. Backend e
        idioma saem da configuração, no daemon.
        """
        return self._call(
            self._core.start_meeting,
            name=(name or "").strip() or None,
            fake=False,
            event_id=(event_id or "").strip() or None,
            diarize=None if diarize is None else bool(diarize),
        )

    def stop_meeting(self, meeting_id: str) -> dict:
        # sem espera: a janela não pode congelar até o engine fechar os .wav;
        # o status vira 'stopping' e o polling da tela mostra o resto.
        return self._call(self._core.stop_meeting, meeting_id=meeting_id, wait_timeout=0.0)

    def pause_meeting(self, meeting_id: str) -> dict:
        return self._call(self._core.pause_meeting, meeting_id=meeting_id)

    def resume_meeting(self, meeting_id: str) -> dict:
        return self._call(self._core.resume_meeting, meeting_id=meeting_id)

    def delete_meeting(self, meeting_id: str) -> dict:
        """Apaga a reunião inteira. A janela confirma antes — aqui não há volta."""
        return self._call(self._core.delete_meeting, meeting_id=meeting_id)

    def list_meetings(self) -> dict:
        """Histórico da tela inicial: uma linha por reunião, mais recentes
        primeiro, já com contagem de segmentos."""
        return self._call(self._core.meeting_summaries)

    def search_meetings(self, query: str, limit: int = 50) -> dict:
        """Busca no catálogo: nome, tags, participantes, notas, resumo, fala."""
        return self._call(self._core.search_meetings, query=query, limit=int(limit))

    def meeting_status(self, meeting_id: str) -> dict:
        return self._call(self._core.meeting_status, meeting_id=meeting_id)

    def transcript(self, meeting_id: str, since_seq: int) -> dict:
        """Segmentos novos do chat. O front-end manda o último seq que já
        desenhou, então o polling ao vivo transporta só o que chegou desde a
        volta anterior — a tela nunca é reconstruída do zero."""
        return self._call(self._core.transcript, meeting_id=meeting_id, since_seq=since_seq)

    def warnings(self, meeting_id: str) -> dict:
        """Falhas que não quebraram a gravação — e por isso passam batido.

        Gravar só o microfone porque o monitor do sistema não abriu não
        interrompe nada: o arquivo sai, a reunião termina, e só depois se
        descobre que os outros participantes não estão na transcrição.
        """
        return self._call(self._core.warnings, meeting_id=meeting_id)

    def audio_info(self, meeting_id: str) -> dict:
        """Onde a janela busca o áudio desta reunião.

        Devolve uma **URL http**, não o caminho no disco: o pywebview serve a
        página por http, e de uma página http o Chromium recusa mídia em
        `file://`. Ver `fireball/gui/audio_server.py`.
        """
        result = self._call(self._core.audio_info, meeting_id=meeting_id)
        if not result["ok"]:
            return result
        info = result["result"]
        name = Path(info["path"]).name if info.get("path") else None
        info["url"] = (
            f"{self._audio_base_url}/{meeting_id}/{name}"
            if name and self._audio_base_url
            else None
        )
        info["exists"] = bool(info["url"])
        return result

    def edit_segment(self, meeting_id: str, seq: int, text: str) -> dict:
        return self._call(self._core.edit_segment, meeting_id=meeting_id, seq=seq, text=text)

    def delete_segment(self, meeting_id: str, seq: int) -> dict:
        return self._call(self._core.delete_segment, meeting_id=meeting_id, seq=seq)

    def summary(self, meeting_id: str) -> dict:
        return self._call(self._core.summary, meeting_id=meeting_id)

    def summarize(self, meeting_id: str, prompt: str = "") -> dict:
        """Gera ou regera o resumo. Sem espera, como o finalize: o provedor
        pode levar minutos, e a janela acompanha o estado pelo polling."""
        return self._call(self._core.summarize, meeting_id=meeting_id, prompt=prompt, wait_timeout=0.0)

    def prompts(self) -> dict:
        return self._call(self._core.prompts)

    def save_prompt(self, name: str, instructions: str, prompt_id: str = "") -> dict:
        return self._call(
            self._core.save_prompt, name=name, instructions=instructions, prompt_id=prompt_id or None
        )

    def delete_prompt(self, prompt_id: str) -> dict:
        return self._call(self._core.delete_prompt, prompt_id=prompt_id)

    def notes(self, meeting_id: str) -> dict:
        return self._call(self._core.notes, meeting_id=meeting_id)

    def save_notes(self, meeting_id: str, text: str) -> dict:
        """A aba Notas salvando o notes.md inteiro (o editor é a versão boa)."""
        return self._call(self._core.save_notes, meeting_id=meeting_id, text=text)

    def rename_meeting(self, meeting_id: str, name: str) -> dict:
        return self._call(self._core.rename_meeting, meeting_id=meeting_id, name=name)

    def set_diarization(self, meeting_id: str, diarize: bool) -> dict:
        return self._call(self._core.set_diarization, meeting_id=meeting_id, diarize=bool(diarize))

    def voices(self) -> dict:
        return self._call(self._core.voice_profiles)

    def enroll_voice(
        self,
        meeting_id: str,
        speaker_key: str,
        name: str,
        email: str = "",
        profile_id: str = "",
    ) -> dict:
        return self._call(
            self._core.enroll_voice,
            meeting_id=meeting_id,
            speaker_key=speaker_key,
            name=name,
            email=email,
            profile_id=profile_id or None,
        )

    def rename_voice(self, profile_id: str, name: str, emails=None) -> dict:
        return self._call(self._core.rename_voice, profile_id=profile_id, name=name, emails=emails)

    def delete_voice(self, profile_id: str) -> dict:
        return self._call(self._core.delete_voice, profile_id=profile_id)

    def finalize(self, meeting_id: str, diarize: bool | None = None) -> dict:
        """Transcrição final pela janela.

        `wait_timeout=0` pelo mesmo motivo do stop: finalizar roda o motor
        sobre o áudio inteiro e pode levar minutos — a janela mostra o status
        'finalizando' pelo polling em vez de congelar. Backend sai da
        configuração, resolvido no daemon.
        """
        return self._call(
            self._core.finalize,
            meeting_id=meeting_id,
            diarize=diarize,
            wait_timeout=0.0,
        )

    def open_folder(self, meeting_id: str) -> dict:
        """Abre a pasta da reunião no gerenciador de arquivos do sistema.

        Não passa pelo core: não é estado do Fireball, é um pedido ao desktop
        de quem está com a janela aberta. `Popen` sem esperar — o navegador de
        arquivos vive a vida dele, e travar a janela até ele fechar seria
        errado.
        """

        def _open() -> dict:
            detail = self._core.meeting_status(meeting_id)
            opener = shutil.which("xdg-open") or shutil.which("open")
            if not opener:
                raise RuntimeError("Nenhum xdg-open/open disponível para abrir a pasta.")
            subprocess.Popen([opener, detail["path"]], start_new_session=True)
            return {"path": detail["path"]}

        return self._call(_open)
