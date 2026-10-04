"""Avisos de reunião esquecida gravando.

A regra mora aqui, sem Qt, sem socket e sem processo: entra um dict de reunião,
uma pasta e as preferências, sai a lista de avisos. Quem decide avisar e como é
o daemon (`DaemonCore`), que é o dono do estado.

Silêncio é medido pelo VAD do próprio Fireball: cada fala fechada vira uma
linha em `transcript.ndjson` com carimbo de relógio (`ts`). Sem fala, sem linha
— então "há quanto tempo ninguém fala" é "há quanto tempo o arquivo não ganha
linha nova", calculado da cauda do arquivo (reunião de 4 h dá MBs de ndjson).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# de quanto em quanto tempo o daemon olha (a regra é barata: uma leitura de cauda)
INTERVALO_S = 30.0

# janela de repetição de cada aviso, em minutos
REPETIR = {"silencio": 15, "relogio": 20, "audio": 30, "pausa": 60}

# abaixo disto não é problema, é respiro entre falas: nas 38 reuniões gravadas
# 90% nunca passam de 4 min de lacuna (o pior caso legítimo medido foi 14,6 min)
AUDIO_PARADO_MIN = 2.0

# tamanho da cauda lida da transcrição
CAUDA_BYTES = 65536


def _parse_iso(valor) -> Optional[datetime]:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutos(quando: float, agora: float) -> float:
    return (agora - quando) / 60.0


def minutos_sem_fala(pasta: Path, agora: float) -> Optional[float]:
    """Há quantos minutos ninguém fala, pelo último `ts` da transcrição.

    Sem transcrição legível, cai na hora de escrita do arquivo — o engine
    escreve nele a cada fala fechada, então a mtime é o mesmo sinal com menos
    precisão. Devolve None quando não há arquivo (nada pra medir).
    """
    arq = pasta / "transcript.ndjson"
    try:
        stat = arq.stat()
    except OSError:
        return None
    if stat.st_size <= CAUDA_BYTES:
        texto = arq.read_text(encoding="utf-8", errors="replace")
    else:
        with arq.open("rb") as fh:
            fh.seek(stat.st_size - CAUDA_BYTES)
            texto = fh.read().decode("utf-8", "replace")
    for linha in reversed(texto.splitlines()):
        linha = linha.strip()
        if not linha.startswith("{"):
            continue                      # a primeira linha da cauda pode vir cortada
        try:
            quando = _parse_iso(json.loads(linha).get("ts"))
        except json.JSONDecodeError:
            continue
        if quando is not None:
            return _minutos(quando.timestamp(), agora)
    return _minutos(stat.st_mtime, agora)


def minutos_sem_audio(pasta: Path, agora: float) -> Optional[float]:
    """Há quantos minutos o áudio parou de crescer (engine morto, mic tomado).

    Vale o track vivo mais recente: o sistema pode estar silencioso sem que
    ninguém tenha morrido.
    """
    idades = []
    for nome in ("mic.pcm", "system.pcm"):
        try:
            idades.append(_minutos((pasta / nome).stat().st_mtime, agora))
        except OSError:
            pass
    return min(idades) if idades else None


class Watch:
    """O estado dos avisos de uma gravação (o que já foi avisado e quando)."""

    def __init__(self) -> None:
        self._reuniao: Optional[str] = None
        self._ultimo: dict[str, float] = {}
        self.ultimo_aviso: Optional[dict] = None

    def _devido(self, chave: str, agora: float) -> bool:
        anterior = self._ultimo.get(chave)
        return anterior is None or (agora - anterior) >= REPETIR[chave] * 60

    def avaliar(self, reuniao: dict, pasta: Path, prefs: dict,
                agora: Optional[float] = None) -> list[dict]:
        """Os avisos que estão valendo agora (já filtrados pela janela de repetição)."""
        agora = time.time() if agora is None else agora
        meeting_id = str(reuniao.get("id") or "")
        if meeting_id != self._reuniao:            # reunião nova: zera os relógios
            self._reuniao, self._ultimo, self.ultimo_aviso = meeting_id, {}, None

        nome = (reuniao.get("name") or "").strip() or "reunião sem nome"
        status = reuniao.get("status") or "recording"
        inicio = _parse_iso(reuniao.get("started_at"))
        minutos_gravando = _minutos(inicio.timestamp(), agora) if inicio else None
        rotulo = f"{nome} · começou às {inicio.astimezone().strftime('%H:%M') if inicio else '?'}"

        if status == "stopping":
            return []

        candidatos: list[dict] = []
        if status == "paused":
            candidatos.append({
                "chave": "pausa",
                "titulo": "Fireball: reunião pausada",
                "corpo": f"{rotulo}\nEstá pausada, mas ainda dona do microfone.\n"
                         "Retomar: fireball resume · Encerrar: fireball stop",
            })
        else:
            transcrevendo = reuniao.get("transcribe_live")
            if transcrevendo is None:
                # reunião criada sem a preferência explícita (caso do engine
                # simulado) não decide nada: vale a configuração
                transcrevendo = prefs.get("transcribe_live", True)
            limite_silencio = prefs.get("aviso_silencio_minutos") or 0
            if transcrevendo and limite_silencio:
                sem_fala = minutos_sem_fala(pasta, agora)
                if sem_fala is not None and sem_fala >= limite_silencio:
                    candidatos.append({
                        "chave": "silencio",
                        "titulo": f"Fireball: {sem_fala:.0f} min sem ninguém falar",
                        "corpo": f"{rotulo}\nA reunião acabou?\n"
                                 "Parar: fireball stop · Pausar: fireball pause",
                    })
            limite_relogio = prefs.get("aviso_minutos") or 0
            if limite_relogio and minutos_gravando is not None and minutos_gravando >= limite_relogio:
                tempo = (f"{minutos_gravando / 60:.1f}".replace(".", ",") + " h"
                         if minutos_gravando >= 60 else f"{minutos_gravando:.0f} min")
                candidatos.append({
                    "chave": "relogio",
                    "titulo": f"Fireball gravando há {tempo}",
                    "corpo": f"{rotulo}\nAinda é isto mesmo? A gravação segue aberta.\n"
                             "Parar: fireball stop",
                })
            parado = minutos_sem_audio(pasta, agora)
            if parado is not None and parado >= AUDIO_PARADO_MIN:
                candidatos.append({
                    "chave": "audio",
                    "titulo": "Fireball: áudio parado",
                    "corpo": f"{rotulo}\nO mic e o monitor não escrevem há {parado:.0f} min "
                             "— o engine caiu?\nConferir: fireball status",
                })

        devidos = [aviso for aviso in candidatos if self._devido(aviso["chave"], agora)]
        for aviso in devidos:
            self._ultimo[aviso["chave"]] = agora
        if devidos:
            self.ultimo_aviso = {
                **devidos[0],
                "quando": datetime.fromtimestamp(agora).isoformat(timespec="seconds"),
            }
        return devidos
