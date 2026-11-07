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

from fireball import catalog, control, settings, storage


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

    # ------------------------------------------------------- preferências

    def test_data_dir_nao_configurado_e_string_vazia(self):
        self.assertEqual(settings.load()["data_dir"], "")

    def test_salvar_data_dir_cria_a_pasta(self):
        disco = Path(self.temp.name) / "disco"
        disco.mkdir()
        settings.save(data_dir=str(disco / "Fireball"))
        self.assertTrue((disco / "Fireball").is_dir())
        self.assertEqual(settings.load()["data_dir"], str(disco / "Fireball"))

    def test_salvar_data_dir_relativo_e_erro(self):
        with self.assertRaises(ValueError):
            settings.save(data_dir="Fireball")

    def test_salvar_data_dir_em_ponto_de_montagem_falso_e_erro(self):
        """`/mnt/ssd2/Fireball` com o ssd2 desmontado criaria a pasta no disco
        principal — o nome do outro disco, no disco errado.

        A home é falsa para o teste não depender de o tempdir cair noutro
        filesystem (o /tmp desta máquina é tmpfs, o TMPDIR do agente fica no
        home): o que a guarda compara é o `st_dev` da home.
        """
        fake_mount = Path(self.temp.name) / "mnt" / "ssd2"
        fake_mount.mkdir(parents=True)
        with patch("pathlib.Path.home", return_value=self.home):
            with self.assertRaises(ValueError) as ctx:
                storage.ensure_data_dir(str(fake_mount / "Fireball"))
        self.assertIn("monte o disco", str(ctx.exception).lower())
        self.assertFalse((fake_mount / "Fireball").exists())

    def test_data_dir_de_disco_ausente_nao_cai_para_o_padrao(self):
        # load() tolera o disco fora (senão o app voltaria a gravar na home em
        # silêncio, que é justamente o que o data_dir existe para impedir)
        self._settings(data_dir=str(Path(self.temp.name) / "desmontado"))
        self.assertEqual(settings.load()["data_dir"], str(Path(self.temp.name) / "desmontado"))

    def test_padrao_vazio_volta_para_a_home(self):
        disco = Path(self.temp.name) / "disco"
        disco.mkdir()
        settings.save(data_dir=str(disco))
        settings.save(data_dir="")
        self.assertEqual(settings.load()["data_dir"], "")
        self.assertEqual(storage.data_root(), self.home)

    # ------------------------------------------------- catálogo e vozes

    def test_catalogo_mora_com_o_acervo(self):
        disco = Path(self.temp.name) / "disco"
        disco.mkdir()
        self._settings(data_dir=str(disco))
        self.assertEqual(catalog.db_path(), disco / "meetings.db")

    def test_disco_fora_nao_zera_o_catalogo(self):
        disco = Path(self.temp.name) / "disco"
        disco.mkdir()
        self._settings(data_dir=str(disco))
        control.create_meeting(
            name="reuniao",
            fake=True,
            mic_device=None,
            system_device=None,
            transcribe=False,
            diarize=False,
            backend="whisper",
            language="pt",
        )  # já com o data_dir valendo
        self.assertEqual(len(control.list_meetings()), 1)
        self._settings(data_dir=str(Path(self.temp.name) / "desmontado"))
        with self.assertRaises(storage.DataDirUnavailable):
            catalog.ensure()
        self._settings(data_dir=str(disco))
        self.assertEqual(len(control.list_meetings()), 1)  # índice intacto

    def test_vozes_moram_com_o_acervo(self):
        disco = Path(self.temp.name) / "disco"
        disco.mkdir()
        self._settings(data_dir=str(disco))
        self.assertEqual(storage.voices_root(), disco / "voices")


if __name__ == "__main__":
    unittest.main()
