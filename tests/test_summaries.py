from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import control, storage, summaries


class SummariesTests(unittest.TestCase):
    """Um resumo por prompt, lado a lado: gerar com outro prompt acrescenta."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()
        meeting = control.create_meeting(
            name="Reunião",
            fake=True,
            mic_device=None,
            system_device=None,
            transcribe=False,
            diarize=False,
            backend="whisper",
            language="pt",
        )
        self.meeting_id = meeting["id"]
        self.dir = storage.meeting_path(self.meeting_id)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _write(self, prompt_id, name, markdown, when):
        return summaries.write(
            self.dir,
            markdown,
            {
                "provider": "claude_code",
                "prompt": prompt_id,
                "prompt_name": name,
                "prompt_instructions": f"instruções de {name}",
                "segments": 10,
                "generated_at": when,
            },
        )

    def test_two_prompts_are_two_summaries(self):
        self._write("padrao", "Resumo padrão", "# Um", "2026-08-30T14:52:00")
        self._write("decisoes", "Só decisões", "# Dois", "2026-08-30T15:04:00")

        listing = control.list_summaries(self.meeting_id)
        self.assertEqual([entry["id"] for entry in listing], ["decisoes", "padrao"])
        self.assertEqual(listing[0]["markdown"].strip(), "# Dois")
        self.assertEqual(listing[0]["prompt_instructions"], "instruções de Só decisões")

    def test_same_prompt_twice_replaces_that_one_only(self):
        self._write("padrao", "Resumo padrão", "# Velho", "2026-08-30T14:52:00")
        self._write("decisoes", "Só decisões", "# Outro", "2026-08-30T15:04:00")
        self._write("padrao", "Resumo padrão", "# Novo", "2026-08-30T15:30:00")

        listing = {entry["id"]: entry["markdown"].strip() for entry in control.list_summaries(self.meeting_id)}
        self.assertEqual(listing, {"padrao": "# Novo", "decisoes": "# Outro"})

    def test_drop_prompt_takes_only_its_own(self):
        self._write("padrao", "Resumo padrão", "# Um", "2026-08-30T14:52:00")
        self._write("decisoes", "Só decisões", "# Dois", "2026-08-30T15:04:00")

        summaries.drop_prompt(self.dir, "padrao")
        self.assertEqual([entry["id"] for entry in control.list_summaries(self.meeting_id)], ["decisoes"])

    def test_root_summary_keeps_the_newest_after_a_delete(self):
        """`summary.md` na raiz é de onde a CLI e o índice de busca leem: ele
        não pode ficar vazio enquanto ainda existe resumo."""
        self._write("padrao", "Resumo padrão", "# Um", "2026-08-30T14:52:00")
        self._write("decisoes", "Só decisões", "# Dois", "2026-08-30T15:04:00")
        self.assertEqual((self.dir / "summary.md").read_text().strip(), "# Dois")

        control.delete_summary(self.meeting_id, "decisoes")
        self.assertEqual([entry["id"] for entry in control.list_summaries(self.meeting_id)], ["padrao"])
        self.assertEqual((self.dir / "summary.md").read_text().strip(), "# Um")

    def test_deleting_the_last_one_clears_the_root(self):
        self._write("padrao", "Resumo padrão", "# Um", "2026-08-30T14:52:00")
        control.delete_summary(self.meeting_id, "padrao")

        self.assertEqual(control.list_summaries(self.meeting_id), [])
        self.assertFalse((self.dir / "summary.md").exists())
        self.assertFalse((self.dir / "summary_result.json").exists())

    def test_summary_written_before_the_store_existed_still_shows_up(self):
        """Reunião resumida antes de existir `summaries/` não pode sumir da aba."""
        (self.dir / "summary.md").write_text("# Legado\n")
        storage.write_json(
            self.dir / "summary_result.json",
            {"provider": "claude_code", "prompt": "padrao", "prompt_name": "Resumo padrão"},
        )

        listing = control.list_summaries(self.meeting_id)
        self.assertEqual([entry["id"] for entry in listing], ["padrao"])
        self.assertEqual(listing[0]["markdown"].strip(), "# Legado")

    def test_a_prompt_id_cannot_escape_the_meeting_folder(self):
        self._write("../../fuga", "Fuga", "# Não", "2026-08-30T14:52:00")
        written = list(summaries.store_dir(self.dir).glob("*.md"))

        self.assertEqual([path.name for path in written], ["fuga.md"])
        self.assertFalse((self.dir.parent / "fuga.md").exists())


if __name__ == "__main__":
    unittest.main()
