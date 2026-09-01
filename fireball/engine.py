"""Motor de transcrição em tempo real.

Roda como processo em background, iniciado por `fireball start`, e escreve cada
segmento reconhecido em transcript.ndjson. `fireball transcript follow` apenas
acompanha esse arquivo — quem produz os dados é este módulo.

Só o modo --fake está implementado por enquanto: gera uma transcrição simulada,
para permitir testar o resto do pipeline (CLI, skill, Monitor, notas, ações)
sem depender de microfone, VAD ou chave de API.
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fireball import audio, realtime, storage
from fireball.backends import (
    BackendUnavailable,
    get_realtime_backend,
    get_streaming_diarization_backend,
)

FAKE_SCRIPT = [
    ("Rafael", "Bom dia pessoal, vamos começar a sincronização do projeto Fireball."),
    ("Rafael", "Hoje quero revisar o que já decidimos sobre a arquitetura de skill e tools."),
    ("Ana", "Eu terminei de revisar a política de autonomia, ficou bem clara."),
    ("Rafael", "Ótimo. Precisamos decidir quem vai cuidar da integração com o Groq para a transcrição final."),
    ("Ana", "Posso ficar com isso, mas só consigo começar semana que vem."),
    ("Rafael", "Perfeito, fica como ação: Ana investigar a API do Groq até sexta."),
    ("Rafael", "Outro ponto: precisamos abrir um ticket no Linear para o checkpoint de resiliência do loop ao vivo."),
    ("Ana", "Concordo, isso é importante antes de qualquer demo."),
    ("Rafael", "Decisão: o Claude só age sozinho para tomar notas, o resto vira pendência de aprovação."),
    ("Ana", "Faz sentido, principalmente para ações externas como Slack e Linear."),
    ("Rafael", "Acho que é isso por hoje. Vamos nos falando pelo Slack."),
]


def _emit(transcript_path: Path, seq: int, speaker: str, text: str) -> None:
    storage.append_ndjson(
        transcript_path,
        {
            "seq": seq,
            "ts": storage.now_iso(),
            "speaker": speaker,
            "text": text,
            "source": "realtime",
        },
    )


def run_fake_engine(meeting_dir: Path, interval: float = 3.0) -> None:
    transcript_path = meeting_dir / "transcript.ndjson"

    # Event.wait() em vez de time.sleep(): desde o PEP 475 o sleep *retoma* o
    # tempo que faltava depois de rodar o handler do sinal, então com
    # --interval alto o engine só notava o SIGTERM segundos depois e o
    # `fireball stop` ficava pendurado esperando. O Event acorda na hora.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # pausado = o roteiro para de andar, como a gravação real para de capturar
    paused = threading.Event()
    signal.signal(signal.SIGUSR1, lambda *_: paused.set())
    signal.signal(signal.SIGUSR2, lambda *_: paused.clear())

    for seq, (speaker, text) in enumerate(FAKE_SCRIPT, start=1):
        while paused.is_set() and not stop.is_set():
            stop.wait(timeout=0.2)
        if stop.is_set():
            break
        _emit(transcript_path, seq, speaker, text)
        if stop.wait(timeout=interval):
            break


def run_real_engine(
    meeting_dir: Path,
    mic_device: Optional[str] = None,
    system_device: Optional[str] = None,
    transcribe_live: bool = True,
    diarize: bool = False,
    backend_name: str = "whisper",
    language: str = realtime.DEFAULT_LANGUAGE,
) -> None:
    """Grava microfone + áudio do sistema (mic.pcm / system.pcm, via parecord)
    até receber SIGTERM/SIGINT, depois empacota tudo em .wav.

    Se `transcribe_live` estiver ligado (padrão), roda a transcrição em tempo
    real em paralelo usando o backend escolhido (ver fireball.backends),
    escrevendo segmentos em transcript.ndjson conforme o áudio chega. Se o
    backend não estiver instalado, a gravação continua normalmente e um
    aviso é registrado em transcribe_warnings.log — `transcript.ndjson` fica
    vazio nesse caso.
    """
    mic_pcm = meeting_dir / "mic.pcm"
    system_pcm = meeting_dir / "system.pcm"
    warnings_path = meeting_dir / "audio_warnings.log"
    rate, channels = audio.DEFAULT_SAMPLERATE, audio.DEFAULT_CHANNELS

    mic_source = mic_device or audio.MIC_DEVICE
    system_source = system_device or audio.SYSTEM_DEVICE

    # parecord não valida o nome do device — um nome errado cai silenciosamente
    # para a fonte padrão. Validamos explicitamente contra o pactl antes de
    # gravar, para não mascarar um dispositivo mal configurado.
    audio.validate_source(mic_source)
    has_system = True
    try:
        audio.validate_source(system_source)
    except audio.AudioBackendUnavailable as exc:
        has_system = False
        warnings_path.write_text(f"Gravando só o microfone — {exc}\n")

    tracks = {
        "mic": (mic_source, mic_pcm, meeting_dir / "mic_parecord.log"),
        "system": (system_source, system_pcm, meeting_dir / "system_parecord.log"),
    }

    def _start(key: str, append: bool):
        source, pcm, log = tracks[key]
        return audio.start_recording(source, pcm, rate, channels, log, append=append)

    procs = {"mic": _start("mic", False), "system": _start("system", False) if has_system else None}

    # dá um instante para o parecord conectar; se a conexão falhar por outro
    # motivo (permissão, device ocupado), o processo já terá saído sozinho.
    time.sleep(0.5)

    if has_system and procs["system"].poll() is not None:
        has_system = False
        err = (meeting_dir / "system_parecord.log").read_text(errors="ignore")
        warnings_path.write_text(
            f"Gravando só o microfone — falha ao abrir o áudio do sistema ({system_source}):\n{err}\n"
        )

    if procs["mic"].poll() is not None:
        err = (meeting_dir / "mic_parecord.log").read_text(errors="ignore")
        raise RuntimeError(f"Falha ao abrir o microfone ({mic_source}):\n{err}")

    state = {"running": True, "want_paused": False, "paused": False}

    def _stop(signum, frame):
        state["running"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # Pausar/retomar chegam por sinal porque o engine é outro processo — mesma
    # via do stop, sem precisar de canal novo. O handler só marca a vontade; a
    # troca acontece no laço, que é onde dá pra fazer isso com calma.
    signal.signal(signal.SIGUSR1, lambda *_: state.__setitem__("want_paused", True))
    signal.signal(signal.SIGUSR2, lambda *_: state.__setitem__("want_paused", False))

    def _apply_pause(paused: bool) -> None:
        """Pausar derruba os parecord; retomar sobe outros, dando append no
        mesmo PCM.

        A primeira tentativa foi SIGSTOP/SIGCONT, que é mais simples — e está
        errada: com o cliente parado o PulseAudio **enfileira** em vez de
        descartar, e no SIGCONT o parecord despeja o acumulado. Medido: 3,7s
        de pausa devolveram ~3,7s de áudio na retomada, ou seja, a pausa só
        atrasava o áudio em vez de descartá-lo — exatamente o oposto do que
        alguém espera ao pausar uma reunião.

        Matando o processo, o que passa durante a pausa não existe em lugar
        nenhum. E como o PCM segue sendo um arquivo contínuo, a posição em
        segundos dentro dele continua válida: a transcrição não precisa
        compensar pausa nenhuma para continuar alinhada com o áudio.
        """
        if paused:
            for proc in procs.values():
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        else:
            procs["mic"] = _start("mic", append=True)
            if has_system:
                procs["system"] = _start("system", append=True)
        state["paused"] = paused

    transcribe_thread = None
    if transcribe_live:
        try:
            backend = get_realtime_backend(backend_name)
            diarization_backend = None
            if diarize:
                try:
                    diarization_backend = get_streaming_diarization_backend()
                    (meeting_dir / "diarization_warnings.log").unlink(missing_ok=True)
                except BackendUnavailable as exc:
                    (meeting_dir / "diarization_warnings.log").write_text(str(exc) + "\n")
            transcribe_thread = threading.Thread(
                target=realtime.run_realtime_transcription,
                args=(meeting_dir, lambda: not state["running"], backend),
                kwargs={
                    "diarization_backend": diarization_backend,
                    "samplerate": rate,
                    "language": language,
                },
                daemon=True,
            )
            transcribe_thread.start()
        except BackendUnavailable as exc:
            (meeting_dir / "transcribe_warnings.log").write_text(str(exc) + "\n")

    while state["running"]:
        if state["want_paused"] != state["paused"]:
            _apply_pause(state["want_paused"])
        time.sleep(0.2)

    # pausado, os parecord já foram encerrados na pausa — só resta fechar o
    # que ainda estiver de pé.
    for proc in procs.values():
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    if transcribe_thread is not None:
        # a gravação já parou (sem mais bytes chegando); dá tempo da thread
        # drenar o que sobrou nas duas tracks e rodar a última transcrição.
        transcribe_thread.join(timeout=60)

    audio.wrap_pcm_as_wav(mic_pcm, meeting_dir / "mic.wav", rate, channels)
    tracks = {"mic": {"device": mic_source, "samplerate": rate, "channels": channels}}
    if has_system:
        audio.wrap_pcm_as_wav(system_pcm, meeting_dir / "system.wav", rate, channels)
        tracks["system"] = {"device": system_source, "samplerate": rate, "channels": channels}
        audio.mix_pcm(mic_pcm, system_pcm, meeting_dir / "meeting.wav", rate, channels)

    storage.write_json(meeting_dir / "audio_tracks.json", tracks)
