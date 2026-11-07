"""A raiz de estado do Fireball pode morar noutro disco (`data_dir`).

Configuração e runtime ficam sempre em `~/.fireball`: o daemon precisa achá-los
antes de ler qualquer preferência. O **estado** — `meetings/`, `meetings.db` e
`voices/` — é o que pode mudar de disco, porque áudio de reunião enche disco.

A invariante que estes testes protegem: raiz de dados configurada e ausente =
disco desmontado → **recusar gravar**, nunca criar a pasta no disco errado.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fireball import storage


class DataRootTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self._env = patch.dict(os.environ, {"FIREBALL_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.temp.cleanup()

    def _settings(self, **values):
        path = self.home / "settings.json"
        path.write_text(json.dumps(values))

    def test_sem_data_dir_o_estado_mora_na_home(self):
        self.assertEqual(storage.data_root(), self.home)
        self.assertEqual(storage.meetings_root(), self.home / "meetings")
        self.assertTrue(storage.data_available())

    def test_data_dir_configurado_manda_no_estado(self):
        disco = Path(self.temp.name) / "disco"
        disco.mkdir()
        self._settings(data_dir=str(disco))
        self.assertEqual(storage.data_root(), disco)
        self.assertEqual(storage.meetings_root(), disco / "meetings")
        self.assertTrue((disco / "meetings").is_dir())

    def test_data_dir_configurado_sem_o_disco_falha_alto(self):
        self._settings(data_dir=str(Path(self.temp.name) / "desmontado"))
        self.assertFalse(storage.data_available())
        with self.assertRaises(storage.DataDirUnavailable) as ctx:
            storage.meetings_root()
        self.assertIn("montado", str(ctx.exception))
        self.assertFalse((Path(self.temp.name) / "desmontado").exists())

    def test_settings_quebrado_nao_derruba_a_leitura(self):
        self._settings(data_dir=123)
        self.assertEqual(storage.data_root(), self.home)


if __name__ == "__main__":
    unittest.main()
