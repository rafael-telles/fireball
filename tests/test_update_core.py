from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fireball import updates
from fireball.daemon.core import DaemonCore


class UpdateCheckTests(unittest.TestCase):
    """A conferência é do daemon: cache com TTL, erro cacheado mais curto."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()
        self.core = DaemonCore()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_cacheia_entre_chamadas(self):
        chamadas = []

        def fake_check(*args, **kwargs):
            chamadas.append(1)
            return {"current": "0.2.0", "latest": "0.2.1", "newer": True, "asset": {}}

        with patch.object(updates, "check", fake_check):
            primeiro = self.core.update_check()
            segundo = self.core.update_check()
        self.assertEqual(len(chamadas), 1, "o poll da janela não pode virar rede")
        self.assertEqual(primeiro, segundo)

    def test_refresh_forca_nova_conferencia(self):
        chamadas = []

        def fake_check(*args, **kwargs):
            chamadas.append(1)
            return {"current": "0.2.0", "latest": "0.2.1", "newer": True, "asset": {}}

        with patch.object(updates, "check", fake_check):
            self.core.update_check()
            self.core.update_check(refresh=True)
        self.assertEqual(len(chamadas), 2)

    def test_erro_vira_estado_visivel_e_nao_excecao(self):
        def explode(*args, **kwargs):
            raise updates.UpdateError("sem rede")

        with patch.object(updates, "check", explode):
            info = self.core.update_check()
        self.assertIn("sem rede", info["error"])
        self.assertIn("checked_at", info)

    def test_status_traz_versao_e_conferencia(self):
        self.core._update = {"current": "0.2.0", "latest": "0.2.1", "newer": True}
        status = self.core.daemon_status()
        self.assertTrue(status["version"])
        self.assertEqual(status["update"]["latest"], "0.2.1")


class UpdateApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": str(self.home)})
        self.env.start()
        self.core = DaemonCore()
        # o AppImage "antigo": um arquivo qualquer que será trocado
        self.alvo = self.dir / "Fireball-0.2.0-x86_64.AppImage"
        self.alvo.write_bytes(b"appimage velho")
        self.novo = self.dir / "origem.AppImage"
        self.novo.write_bytes(b"appimage novo, com a versao 0.2.1")
        self.asset = {
            "name": "Fireball-0.2.1-x86_64.AppImage",
            "url": self.novo.as_uri(),
            "size": self.novo.stat().st_size,
            "sha256": hashlib.sha256(self.novo.read_bytes()).hexdigest(),
        }

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_recusa_rodando_do_fonte(self):
        with patch.dict(os.environ, {"APPIMAGE": ""}):
            with self.assertRaises(RuntimeError) as ctx:
                self.core.update_apply()
        self.assertIn("código-fonte", str(ctx.exception))

    def test_recusa_com_reuniao_gravando(self):
        with patch.dict(os.environ, {"APPIMAGE": str(self.alvo)}):
            self.core._recording = object()  # type: ignore[assignment]  # não-None = "gravando"
            with self.assertRaises(RuntimeError) as ctx:
                self.core.update_apply()
        self.assertIn("gravando", str(ctx.exception))

    def test_nao_baixa_se_ja_esta_na_ultima(self):
        with patch.dict(os.environ, {"APPIMAGE": str(self.alvo)}), patch.object(
            updates, "check", lambda *a, **k: {"current": "0.2.1", "latest": "0.2.1", "newer": False, "asset": None}
        ):
            resultado = self.core.update_apply()
        self.assertFalse(resultado["updated"])
        self.assertEqual(self.alvo.read_bytes(), b"appimage velho")

    def test_troca_o_appimage_e_marca_a_versao_nova(self):
        with patch.dict(os.environ, {"APPIMAGE": str(self.alvo)}), patch.object(
            updates, "check", lambda *a, **k: {"current": "0.2.0", "latest": "0.2.1", "newer": True, "asset": self.asset}
        ), patch.object(self.core, "_restart_after_update") as reinicio:
            resultado = self.core.update_apply()
        self.assertTrue(resultado["updated"])
        self.assertEqual(resultado["version"], "0.2.1")
        self.assertEqual(self.alvo.read_bytes(), self.novo.read_bytes())
        self.assertTrue(os.access(self.alvo, os.X_OK))
        reinicio.assert_called_once()
        self.assertFalse(self.core.update_check()["newer"], "depois de aplicar não oferece de novo")

    def test_erro_de_conferencia_nao_troca_nada(self):
        with patch.dict(os.environ, {"APPIMAGE": str(self.alvo)}), patch.object(
            updates, "check", lambda *a, **k: (_ for _ in ()).throw(updates.UpdateError("sem rede"))
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.core.update_apply()
        self.assertIn("sem rede", str(ctx.exception))
        self.assertEqual(self.alvo.read_bytes(), b"appimage velho")


if __name__ == "__main__":
    unittest.main()
