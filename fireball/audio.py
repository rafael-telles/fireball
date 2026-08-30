"""Captura de áudio real via `parecord` (PulseAudio/PipeWire): microfone e
áudio do sistema — o que está tocando nos alto-falantes/fone, ou seja, os
outros participantes da reunião.

Testado nesta máquina: o PortAudio (sounddevice) só expõe dispositivos
agregados genéricos ("pulse", "pipewire"), não cada fonte individual — não
dá para mirar especificamente no monitor da saída padrão através dele. O
`parecord` resolve isso de forma nativa com os nomes especiais
`@DEFAULT_SOURCE@` (microfone padrão) e `@DEFAULT_MONITOR@` (monitor do sink
de saída padrão).

Cada track grava PCM cru continuamente em disco — resiliente a interrupção,
já que não há cabeçalho de arquivo para corromper se o processo morrer no
meio. `wrap_pcm_as_wav` / `mix_pcm` convertem/combinam os .pcm depois, a
qualquer momento.
"""

from __future__ import annotations

import array
import json
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_SAMPLERATE = 16000
DEFAULT_CHANNELS = 1

MIC_DEVICE = "@DEFAULT_SOURCE@"
SYSTEM_DEVICE = "@DEFAULT_MONITOR@"


class AudioBackendUnavailable(RuntimeError):
    pass


def _require_binary(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        raise AudioBackendUnavailable(f"`{name}` não encontrado no PATH. {hint}")


def _require_parecord() -> None:
    _require_binary(
        "parecord",
        "Instale as utilidades de linha de comando do PulseAudio/PipeWire-pulse "
        "(pacote costuma se chamar `pulseaudio-utils` ou `libpulse`).",
    )


def list_sources() -> list[dict]:
    """Lista as fontes de áudio (microfones e monitores de saída) via `pactl`.

    Fontes cujo nome termina em '.monitor' são o que está tocando no sistema
    (útil para escolher explicitamente o áudio dos outros participantes); as
    demais são entradas de microfone de verdade.
    """
    _require_binary("pactl", "Faz parte do pacote do PulseAudio/PipeWire-pulse.")
    out = subprocess.run(
        ["pactl", "-f", "json", "list", "sources"],
        capture_output=True,
        text=True,
        check=True,
    )
    sources = json.loads(out.stdout)
    return [
        {
            "name": s["name"],
            "description": s.get("description", ""),
            "is_monitor": s["name"].endswith(".monitor"),
            "state": s.get("state"),
        }
        for s in sources
    ]


def validate_source(name: str) -> None:
    """Levanta AudioBackendUnavailable se `name` não for um dos tokens
    especiais nem uma fonte real conhecida pelo pactl.

    Necessário porque `parecord --device=<nome inválido>` não falha: ele cai
    silenciosamente para a fonte padrão, o que mascararia um nome de
    dispositivo errado como se estivesse tudo funcionando.
    """
    if name in (MIC_DEVICE, SYSTEM_DEVICE):
        return
    known = {s["name"] for s in list_sources()}
    if name not in known:
        raise AudioBackendUnavailable(
            f"Fonte de áudio '{name}' não encontrada. Rode `fireball devices` para "
            "ver os nomes válidos (ou use @DEFAULT_SOURCE@ / @DEFAULT_MONITOR@)."
        )


@dataclass
class TrackConfig:
    device: str
    samplerate: int
    channels: int


# quanto áudio o parecord segura antes de escrever (ver start_recording)
LATENCY_MS = 200


def start_recording(
    device: str,
    pcm_path: Path,
    samplerate: int = DEFAULT_SAMPLERATE,
    channels: int = DEFAULT_CHANNELS,
    stderr_path: Optional[Path] = None,
    append: bool = False,
) -> subprocess.Popen:
    """Sobe um `parecord` gravando PCM cru contínuo em pcm_path até ser
    terminado (SIGTERM). Não bloqueia — devolve o Popen para o chamador
    gerenciar o ciclo de vida.

    O parecord escreve no **stdout** (que redirecionamos para o arquivo) em vez
    de receber o caminho como argumento, para que `append=True` seja possível:
    é assim que retomar uma gravação pausada continua o mesmo PCM, em vez de
    truncá-lo. Um PCM contínuo é o que mantém a conta de "segundo tal da
    gravação" válida do começo ao fim.
    """
    _require_parecord()
    stderr = open(stderr_path, "ab" if append else "wb") if stderr_path else subprocess.DEVNULL
    out = open(pcm_path, "ab" if append else "wb")
    try:
        return subprocess.Popen(
            [
                "parecord",
                f"--device={device}",
                f"--rate={samplerate}",
                f"--channels={channels}",
                "--format=s16le",
                "--raw",
                # Sem isto o parecord escreve em blocos de ~2s. Dois problemas:
                # o que estiver no bloco em voo se perde quando o processo é
                # encerrado (na pausa, media-se ~2s de áudio sumindo por
                # ciclo), e a transcrição ao vivo só vê o áudio 2s depois de
                # falado. Com 200ms, a perda por pausa cai para o
                # imperceptível e a fala chega ao VAD quase na hora.
                f"--latency-msec={LATENCY_MS}",
            ],
            stdout=out,
            stderr=stderr,
        )
    finally:
        out.close()  # o filho herdou o fd; o nosso não serve pra mais nada


def wrap_pcm_as_wav(pcm_path: Path, wav_path: Path, samplerate: int, channels: int, sampwidth: int = 2) -> None:
    if not pcm_path.exists() or pcm_path.stat().st_size == 0:
        return
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(samplerate)
        wf.writeframes(pcm_path.read_bytes())


def mix_pcm(mic_pcm: Path, system_pcm: Path, out_wav: Path, samplerate: int, channels: int) -> None:
    """Mixagem simples (soma com clipping) das duas tracks, truncando no menor
    comprimento. As duas rodam com o mesmo samplerate/canais por construção
    (pedidos explicitamente ao parecord nas duas chamadas).

    Laço puro em Python (sem numpy) — ok para o sketch, mas O(n): para
    reuniões muito longas, considerar numpy se isso ficar lento.
    """
    if not mic_pcm.exists() or not system_pcm.exists():
        return

    mic_samples = array.array("h", mic_pcm.read_bytes())
    sys_samples = array.array("h", system_pcm.read_bytes())
    n = min(len(mic_samples), len(sys_samples))
    if n == 0:
        return

    mixed = array.array("h", bytes(n * 2))
    for i in range(n):
        mixed[i] = max(-32768, min(32767, mic_samples[i] + sys_samples[i]))

    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(mixed.tobytes())
