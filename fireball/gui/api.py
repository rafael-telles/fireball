"""Ponte entre o JavaScript da janela e o núcleo do daemon.

A janela é renderizada **pelo próprio daemon** (ver `fireball.gui.shell`), então
aqui chamamos o `DaemonCore` direto, em processo — não faria sentido o daemon
abrir um socket para falar consigo mesmo. Continua havendo uma autoridade só:
todo método passa pelo mesmo core, sob o mesmo lock que serializa a CLI.

O envelope `{ok, result}` / `{ok, error}` é o mesmo que o socket devolve, para
o front-end (`web/app.js`) não precisar saber por onde a chamada veio.
"""

from __future__ import annotations

from fireball.backends import BATCH_BACKENDS, REALTIME_BACKENDS
from fireball.daemon.core import DaemonCore


class Api:
    def __init__(self, core: DaemonCore):
        self._core = core

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
        return {"realtime": list(REALTIME_BACKENDS), "final": list(BATCH_BACKENDS)}

    def get_settings(self) -> dict:
        return self._call(self._core.get_settings)

    def save_settings(self, values: dict) -> dict:
        return self._call(self._core.update_settings, **values)

    def start_meeting(self, name: str) -> dict:
        """Começar reunião pela janela é uma decisão só: o nome.

        `fake=False` fixo — o motor simulado existe pra testar o pipeline sem
        microfone, o que é trabalho de desenvolvimento (`fireball start
        --fake`), não escolha de quem abriu a janela pra gravar. Backend e
        idioma saem da configuração, no daemon.
        """
        return self._call(self._core.start_meeting, name=name, fake=False)

    def stop_meeting(self, meeting_id: str) -> dict:
        # sem espera: a janela não pode congelar até o engine fechar os .wav;
        # o status vira 'stopping' e o polling da tela mostra o resto.
        return self._call(self._core.stop_meeting, meeting_id=meeting_id, wait_timeout=0.0)

    def list_meetings(self) -> dict:
        """Histórico da tela inicial: uma linha por reunião, mais recentes
        primeiro, já com contagem de segmentos e se existe transcrição final."""
        return self._call(self._core.meeting_summaries)

    def meeting_status(self, meeting_id: str) -> dict:
        return self._call(self._core.meeting_status, meeting_id=meeting_id)

    def transcript(self, meeting_id: str, since_seq: int, source: str) -> dict:
        """Segmentos novos do chat. O front-end manda o último seq que já
        desenhou, então o polling ao vivo transporta só o que chegou desde a
        volta anterior — a tela nunca é reconstruída do zero."""
        return self._call(self._core.transcript, meeting_id=meeting_id, since_seq=since_seq, source=source)
