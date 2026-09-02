from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import control, storage


class TranscriptMetaTests(unittest.TestCase):
    """Qual transcrição está na tela, e com que motor ela foi feita."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()
        meeting = control.create_meeting(
            name="Planejamento",
            fake=False,
            mic_device=None,
            system_device=None,
            transcribe=True,
            diarize=False,
            backend="whisper",
            language="pt",
        )
        self.meeting_id = meeting["id"]
        self.dir = storage.meeting_path(self.meeting_id)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _live(self):
        storage.append_ndjson(
            self.dir / "transcript.ndjson",
            {"seq": 1, "ts": "2026-08-30T14:03:00", "speaker": "Você", "text": "primeira fala"},
        )

    def test_empty_transcript_says_so(self):
        meta = control.transcript_meta(self.meeting_id)
        self.assertEqual(meta["kind"], "empty")
        self.assertEqual(meta["segments"], 0)
        self.assertIsNone(meta["generated_at"])

    def test_without_a_finalize_the_text_is_the_live_one(self):
        self._live()
        meta = control.transcript_meta(self.meeting_id)

        self.assertEqual(meta["kind"], "live")
        self.assertEqual(meta["backend"], "whisper")
        self.assertEqual(meta["segments"], 1)
        self.assertIsNotNone(meta["generated_at"])

    def test_after_a_finalize_the_backend_is_the_one_that_rewrote_it(self):
        self._live()
        storage.write_json(
            self.dir / "finalize_result.json",
            {"segments": 214, "backend": "groq", "replaced": True, "diarized": True},
        )
        meta = control.transcript_meta(self.meeting_id)

        self.assertEqual(meta["kind"], "final")
        self.assertEqual(meta["backend"], "groq")
        self.assertEqual(meta["segments"], 214)
        self.assertTrue(meta["diarized"])

    def test_a_finalize_that_replaced_nothing_leaves_the_live_text_in_place(self):
        """Backend que devolveu zero segmento não troca a transcrição — então a
        barra não pode dizer que o texto na tela é o final."""
        self._live()
        storage.write_json(
            self.dir / "finalize_result.json",
            {"segments": 0, "backend": "groq", "replaced": False, "diarized": False},
        )
        self.assertEqual(control.transcript_meta(self.meeting_id)["kind"], "live")

    def test_export_writes_readable_markdown(self):
        storage.append_ndjson(
            self.dir / "transcript.ndjson",
            {"seq": 1, "start": 134.0, "end": 148.0, "speaker": "Marina Alves", "text": "Fechando o escopo."},
        )
        storage.append_ndjson(
            self.dir / "transcript.ndjson",
            {"seq": 2, "start": 3700.5, "end": 3720.0, "speaker": "Você", "text": "Combinado."},
        )
        result = control.export_transcript(self.meeting_id)
        text = (self.dir / "transcript.md").read_text()

        self.assertEqual(result["segments"], 2)
        self.assertIn("# Planejamento", text)
        self.assertIn("**Marina Alves** · `02:14`", text)
        # passando de uma hora o carimbo ganha a hora, senão 1:01:40 viraria 61:40
        self.assertIn("**Você** · `1:01:40`", text)
        self.assertIn("Combinado.", text)

    def test_export_refuses_a_meeting_with_nothing_to_export(self):
        with self.assertRaises(ValueError):
            control.export_transcript(self.meeting_id)


if __name__ == "__main__":
    unittest.main()
