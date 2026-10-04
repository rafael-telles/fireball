from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fireball import storage, watch

AGORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc).timestamp()


class WatchTests(unittest.TestCase):
    """A regra, sem daemon e sem sleep: o tempo entra por parâmetro."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()
        self.pasta = Path(self.temp.name) / "meetings" / "20261004-110000-reuniao"
        self.pasta.mkdir(parents=True)
        self.prefs = {"aviso_minutos": 75, "aviso_silencio_minutos": 10}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _reuniao(self, minutos=30, status="recording", transcribe_live: bool | None = True, fake=False):
        inicio = datetime.fromtimestamp(AGORA - minutos * 60, tz=timezone.utc)
        return {
            "id": "20261004-110000-reuniao",
            "name": "Reunião de teste",
            "started_at": inicio.isoformat(),
            "status": status,
            "transcribe_live": transcribe_live,
            "fake": fake,
        }

    def _fala(self, minutos_atras: float):
        quando = datetime.fromtimestamp(AGORA - minutos_atras * 60, tz=timezone.utc)
        storage.append_ndjson(
            self.pasta / "transcript.ndjson",
            {"seq": 1, "ts": quando.isoformat(), "start": 0.0, "end": 1.0,
             "speaker": "Outros participantes", "text": "oi", "source": "realtime"},
        )

    def test_silencio_curto_nao_avisa(self):
        self._fala(4)                                   # p90 real das reuniões
        self.assertEqual(watch.Watch().avaliar(self._reuniao(30), self.pasta, self.prefs, AGORA), [])

    def test_silencio_longo_avisa(self):
        self._fala(25)
        avisos = watch.Watch().avaliar(self._reuniao(40), self.pasta, self.prefs, AGORA)
        self.assertEqual([a["chave"] for a in avisos], ["silencio"])

    def test_nao_repete_antes_da_janela(self):
        self._fala(25)
        vigia = watch.Watch()
        self.assertTrue(vigia.avaliar(self._reuniao(40), self.pasta, self.prefs, AGORA))
        self.assertEqual(vigia.avaliar(self._reuniao(45), self.pasta, self.prefs, AGORA + 300), [])
        self.assertTrue(vigia.avaliar(self._reuniao(60), self.pasta, self.prefs, AGORA + 16 * 60))

    def test_desligado_por_configuracao(self):
        self._fala(120)
        prefs = {"aviso_minutos": 0, "aviso_silencio_minutos": 0}
        self.assertEqual(watch.Watch().avaliar(self._reuniao(180), self.pasta, prefs, AGORA), [])

    def test_relogio(self):
        self._fala(0)
        avisos = watch.Watch().avaliar(self._reuniao(90), self.pasta, self.prefs, AGORA)
        self.assertEqual([a["chave"] for a in avisos], ["relogio"])

    def test_sem_transcricao_nao_avisa_silencio(self):
        reuniao = self._reuniao(40, transcribe_live=False)
        self.assertEqual(watch.Watch().avaliar(reuniao, self.pasta, self.prefs, AGORA), [])

    def test_transcricao_nula_cai_na_configuracao(self):
        """Reunião sem a preferência gravada (engine simulado grava null) não decide nada."""
        self._fala(25)
        reuniao = self._reuniao(40, transcribe_live=None)
        self.assertEqual(
            [a["chave"] for a in watch.Watch().avaliar(reuniao, self.pasta, self.prefs, AGORA)],
            ["silencio"],
        )
        self.assertEqual(
            watch.Watch().avaliar(reuniao, self.pasta,
                                  {"aviso_minutos": 0, "aviso_silencio_minutos": 10,
                                   "transcribe_live": False}, AGORA),
            [],
        )

    def test_titulo_do_relogio_usa_minutos_ate_uma_hora(self):
        self._fala(0)
        avisos = watch.Watch().avaliar(self._reuniao(40), self.pasta,
                                       {"aviso_minutos": 30, "aviso_silencio_minutos": 0}, AGORA)
        self.assertEqual([a["titulo"] for a in avisos], ["Fireball gravando há 40 min"])

    def test_pausada_lembra(self):
        self._fala(0)
        avisos = watch.Watch().avaliar(self._reuniao(20, status="paused"), self.pasta, self.prefs, AGORA)
        self.assertEqual([a["chave"] for a in avisos], ["pausa"])

    def test_stopping_nao_avisa(self):
        self._fala(60)
        self.assertEqual(watch.Watch().avaliar(self._reuniao(90, status="stopping"),
                                               self.pasta, self.prefs, AGORA), [])

    def test_le_a_cauda_de_transcricao_grande(self):
        grande = self.pasta / "transcript.ndjson"
        with grande.open("w", encoding="utf-8") as fh:      # > 64 KB de falas antigas
            for _ in range(4000):
                fh.write(json.dumps({"seq": 1, "ts": "2026-10-04T10:00:00+00:00",
                                     "text": "l" * 40}) + "\n")
        self._fala(25)
        avisos = watch.Watch().avaliar(self._reuniao(40), self.pasta, self.prefs, AGORA)
        self.assertEqual([a["chave"] for a in avisos], ["silencio"])

    def test_reuniao_nova_zera_os_avisos(self):
        self._fala(25)
        vigia = watch.Watch()
        self.assertTrue(vigia.avaliar(self._reuniao(40), self.pasta, self.prefs, AGORA))
        outra = self._reuniao(40)
        outra["id"] = "20261004-113000-outra"
        self.assertTrue(vigia.avaliar(outra, self.pasta, self.prefs, AGORA))
