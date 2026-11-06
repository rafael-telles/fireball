from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from fireball import updates

ASSET_URL = "https://example.test/Fireball-0.2.1-x86_64.AppImage"


def payload(**over):
    data = {
        "tag_name": "v0.2.1",
        "html_url": "https://github.com/rafael-telles/fireball/releases/tag/v0.2.1",
        "body": "notas da release",
        "published_at": "2026-11-06T22:09:14Z",
        "assets": [
            {"name": "source.tar.gz", "browser_download_url": "https://example.test/src", "size": 10},
            {
                "name": "Fireball-0.2.1-x86_64.AppImage",
                "browser_download_url": ASSET_URL,
                "size": 348162552,
                "digest": "sha256:" + "a" * 64,
            },
        ],
    }
    data.update(over)
    return data


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, _size: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(body: bytes):
    def _opener(request, timeout=None):
        return FakeResponse(body)

    return _opener


class VersionTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(updates.parse_version("v0.2.1"), (0, 2, 1))
        self.assertEqual(updates.parse_version("0.10.0"), (0, 10, 0))
        self.assertEqual(updates.parse_version("0.2.0-rc1"), (0, 2, 0, 1))
        self.assertEqual(updates.parse_version(""), ())

    def test_is_newer(self):
        self.assertTrue(updates.is_newer("v0.2.1", "0.2.0"))
        self.assertTrue(updates.is_newer("0.10.0", "0.9.9"), "0.10 é depois de 0.9")
        self.assertFalse(updates.is_newer("v0.2.0", "0.2.0"))
        self.assertFalse(updates.is_newer("0.1.9", "0.2.0"))

    def test_versao_do_pyproject_no_checkout(self):
        """Instalação editable envelhece o metadado; no checkout vale o arquivo."""
        with tempfile.TemporaryDirectory() as temp:
            arquivo = Path(temp) / "pyproject.toml"
            arquivo.write_text('[project]\nname = "fireball"\nversion = "9.9.9"\n')
            self.assertEqual(updates.version_from_pyproject(arquivo), "9.9.9")
            self.assertEqual(updates.version_from_pyproject(Path(temp) / "nao-existe.toml"), "")

    def test_version_from_asset(self):
        self.assertEqual(updates.version_from_asset("Fireball-0.2.1-x86_64.AppImage"), "0.2.1")
        self.assertEqual(updates.version_from_asset("Fireball.AppImage"), "")


class ReleaseInfoTests(unittest.TestCase):
    def test_pega_o_appimage_e_o_digest(self):
        info = updates.release_info(payload(), current="0.2.0")
        self.assertEqual(info["latest"], "0.2.1")
        self.assertTrue(info["newer"])
        self.assertEqual(info["asset"]["url"], ASSET_URL)
        self.assertEqual(info["asset"]["sha256"], "a" * 64)
        self.assertIn("notas da release", info["notes"])

    def test_release_sem_appimage_nao_quebra(self):
        """O check continua válido; quem aplica é que recusa."""
        info = updates.release_info(payload(assets=[]), current="0.2.0")
        self.assertTrue(info["newer"])
        self.assertIsNone(info["asset"])

    def test_sem_tag_e_erro(self):
        with self.assertRaises(updates.UpdateError):
            updates.release_info({"assets": []})
        with self.assertRaises(updates.UpdateError):
            updates.release_info("nada")  # type: ignore[arg-type]

    def test_versao_igual_nao_e_novidade(self):
        info = updates.release_info(payload(), current="0.2.1")
        self.assertFalse(info["newer"])

    def test_check_bate_no_endpoint_certo(self):
        visto = {}

        def opener(request, timeout=None):
            visto["url"] = request.full_url
            visto["ua"] = request.get_header("User-agent")
            return FakeResponse(json.dumps(payload()).encode())

        info = updates.check(current="0.2.0", opener=opener)
        self.assertEqual(visto["url"], updates.RELEASES_URL)
        self.assertTrue(visto["ua"].startswith("fireball/"))
        self.assertEqual(info["latest"], "0.2.1")

    def test_check_sem_release_publicada(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)  # type: ignore[arg-type]

        with self.assertRaises(updates.UpdateError) as ctx:
            updates.check(opener=opener)
        self.assertIn("não tem release", str(ctx.exception))

    def test_check_sem_rede(self):
        def opener(request, timeout=None):
            raise urllib.error.URLError("sem rota")

        with self.assertRaises(updates.UpdateError):
            updates.check(opener=opener)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.source = self.dir / "origem.AppImage"
        self.source.write_bytes(b"conteudo do appimage novo")
        self.asset = {
            "name": "Fireball-0.2.1-x86_64.AppImage",
            "url": self.source.as_uri(),
            "size": self.source.stat().st_size,
            "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
        }
        self.dest = self.dir / "Fireball-0.2.0-x86_64.AppImage"
        self.dest.write_bytes(b"appimage velho")

    def tearDown(self):
        self.temp.cleanup()

    def test_baixa_conferindo_e_troca_o_arquivo(self):
        updates.download(self.asset, self.dest)
        self.assertEqual(self.dest.read_bytes(), self.source.read_bytes())
        self.assertTrue(os.access(self.dest, os.X_OK), "o AppImage novo tem que sair executável")
        self.assertFalse((self.dest.parent / (self.dest.name + ".part")).exists())

    def test_sha_errado_nao_troca_nada(self):
        ruim = dict(self.asset, sha256="b" * 64)
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.download(ruim, self.dest)
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertEqual(self.dest.read_bytes(), b"appimage velho", "o que funciona fica no lugar")

    def test_tamanho_errado_nao_troca_nada(self):
        ruim = dict(self.asset, size=self.asset["size"] + 1)
        with self.assertRaises(updates.UpdateError):
            updates.download(ruim, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"appimage velho")

    def test_sem_url_e_erro(self):
        with self.assertRaises(updates.UpdateError):
            updates.download({"name": "x"}, self.dest)

    def test_progresso_conta_os_bytes(self):
        visto = []
        updates.download(self.asset, self.dest, progress=lambda done, total: visto.append((done, total)))
        self.assertEqual(visto[-1], (self.asset["size"], self.asset["size"]))

    def test_apply_recusa_pasta_sem_escrita(self):
        asset = dict(self.asset, url="https://example.test/x")
        with patch("fireball.updates.os.access", return_value=False):
            with self.assertRaises(updates.UpdateError) as ctx:
                updates.apply(asset, self.dest)
        self.assertIn("permissão", str(ctx.exception).lower())

    def test_apply_devolve_versao_do_asset(self):
        resultado = updates.apply(self.asset, self.dest)
        self.assertEqual(resultado["version"], "0.2.1")
        self.assertEqual(Path(resultado["path"]), self.dest)


class AppImageTests(unittest.TestCase):
    def test_sem_appimage_quando_roda_do_fonte(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(updates.appimage_path())

    def test_appimage_ausente_no_disco_nao_conta(self):
        with patch.dict(os.environ, {"APPIMAGE": "/nao/existe/Fireball.AppImage"}):
            self.assertIsNone(updates.appimage_path())

    def test_appimage_existente(self):
        with tempfile.TemporaryDirectory() as temp:
            arquivo = Path(temp) / "Fireball.AppImage"
            arquivo.write_bytes(b"x")
            with patch.dict(os.environ, {"APPIMAGE": str(arquivo)}):
                self.assertEqual(updates.appimage_path(), arquivo)


if __name__ == "__main__":
    unittest.main()
