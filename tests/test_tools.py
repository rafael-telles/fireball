from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fireball import tools


class FindBinaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_binary(self, directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_path_wins(self):
        """Quem exportou PATH escolheu; o plano B não passa na frente."""
        on_path = self.make_binary(self.root / "path", "gog")
        fallback = self.root / "fallback"
        self.make_binary(fallback, "gog")

        with patch.dict(os.environ, {"PATH": str(on_path.parent)}), patch.object(
            tools, "FALLBACK_DIRS", (str(fallback),)
        ):
            self.assertEqual(tools.find_binary("gog"), str(on_path))

    def test_fallback_dir_when_path_is_short(self):
        """O caso do lançador do desktop: binário instalado, PATH sem ele."""
        fallback = self.root / "fallback"
        installed = self.make_binary(fallback, "gog")

        with patch.dict(os.environ, {"PATH": str(self.root / "vazio")}), patch.object(
            tools, "FALLBACK_DIRS", (str(fallback),)
        ):
            self.assertEqual(tools.find_binary("gog"), str(installed))

    def test_env_override_comes_first(self):
        override = self.root / "override"
        chosen = self.make_binary(override, "gog")
        fallback = self.root / "fallback"
        self.make_binary(fallback, "gog")

        env = {"PATH": str(self.root / "vazio"), tools.PATH_ENV: str(override)}
        with patch.dict(os.environ, env), patch.object(
            tools, "FALLBACK_DIRS", (str(fallback),)
        ):
            self.assertEqual(tools.find_binary("gog"), str(chosen))

    def test_missing_binary_is_none(self):
        with patch.dict(os.environ, {"PATH": str(self.root / "vazio")}), patch.object(
            tools, "FALLBACK_DIRS", (str(self.root / "tambem-vazio"),)
        ):
            self.assertIsNone(tools.find_binary("gog"))

    def test_directory_with_the_name_is_not_a_binary(self):
        fallback = self.root / "fallback"
        (fallback / "gog").mkdir(parents=True)

        with patch.dict(os.environ, {"PATH": str(self.root / "vazio")}), patch.object(
            tools, "FALLBACK_DIRS", (str(fallback),)
        ):
            self.assertIsNone(tools.find_binary("gog"))

    def test_non_executable_file_is_not_a_binary(self):
        fallback = self.root / "fallback"
        fallback.mkdir(parents=True)
        (fallback / "gog").write_text("sem bit de execução\n")

        with patch.dict(os.environ, {"PATH": str(self.root / "vazio")}), patch.object(
            tools, "FALLBACK_DIRS", (str(fallback),)
        ):
            self.assertIsNone(tools.find_binary("gog"))


if __name__ == "__main__":
    unittest.main()
