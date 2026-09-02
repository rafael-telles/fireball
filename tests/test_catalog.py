from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import catalog, control, storage


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _create(self, name=None, event=None):
        return control.create_meeting(
            name=name,
            fake=True,
            mic_device=None,
            system_device=None,
            transcribe=False,
            diarize=False,
            backend="whisper",
            language="pt",
            event=event,
        )

    def test_rebuild_from_folders(self):
        first = self._create(name="Sincronização Fireball")
        second = self._create(name="1:1 com Ana")
        catalog.db_path().unlink()
        count = catalog.rebuild()
        self.assertEqual(count, 2)
        ids = {row["id"] for row in catalog.list_meetings()}
        self.assertEqual(ids, {first["id"], second["id"]})

    def test_search_name_tags_notes_and_speech(self):
        meeting = self._create(name="Planning de contratação")
        meeting_id = meeting["id"]
        control.write_meeting_fields(meeting_id, tags=["recrutamento", "produto"])
        control.write_notes(meeting_id, "# Notas\n\nFalar do orçamento de vagas.\n")
        storage.append_ndjson(
            storage.meeting_path(meeting_id) / "transcript.ndjson",
            {"seq": 1, "speaker": "Rafael", "text": "Vamos abrir a vaga de design."},
        )
        catalog.replace_transcript(meeting_id)

        by_name = catalog.search("contratação")
        self.assertEqual([row["id"] for row in by_name["meetings"]], [meeting_id])
        self.assertIn("name", by_name["meetings"][0]["matched"])

        by_tag = catalog.search("recrutamento")
        self.assertEqual(by_tag["meetings"][0]["id"], meeting_id)
        self.assertIn("tags", by_tag["meetings"][0]["matched"])

        by_note = catalog.search("orçamento")
        self.assertEqual(by_note["meetings"][0]["id"], meeting_id)
        self.assertIn("notes", by_note["meetings"][0]["matched"])

        by_speech = catalog.search("vaga de design")
        self.assertEqual(by_speech["meetings"][0]["id"], meeting_id)
        self.assertIn("transcript", by_speech["meetings"][0]["matched"])
        self.assertEqual(by_speech["meetings"][0]["hits"][0]["seq"], 1)

    def test_search_attendee(self):
        meeting = self._create(
            name="Alinhamento",
            event={
                "title": "Alinhamento",
                "attendees": [{"name": "Ana Souza", "email": "ana@escola.com"}],
            },
        )
        hits = catalog.search("Ana Souza")
        self.assertEqual(hits["meetings"][0]["id"], meeting["id"])
        self.assertIn("attendees", hits["meetings"][0]["matched"])

    def test_replace_transcript_after_finalize_rewrite(self):
        meeting = self._create(name="Daily")
        meeting_id = meeting["id"]
        path = storage.meeting_path(meeting_id) / "transcript.ndjson"
        storage.append_ndjson(path, {"seq": 1, "speaker": "Rafael", "text": "assunto antigo"})
        catalog.replace_transcript(meeting_id)
        self.assertTrue(catalog.search("antigo")["meetings"])

        path.write_text("")
        storage.append_ndjson(path, {"seq": 1, "speaker": "Ana", "text": "assunto novo da sprint"})
        catalog.replace_transcript(meeting_id)

        self.assertFalse(catalog.search("antigo")["meetings"])
        found = catalog.search("sprint")
        self.assertEqual(found["meetings"][0]["id"], meeting_id)
        self.assertEqual(found["meetings"][0]["hits"][0]["speaker"], "Ana")
        summaries = catalog.meeting_summaries()
        self.assertEqual(summaries[0]["segments"], 1)

    def test_delete_removes_from_index(self):
        meeting = self._create(name="Reunião a apagar")
        self.assertTrue(catalog.search("apagar")["meetings"])
        control.delete_meeting(meeting["id"])
        self.assertFalse(catalog.search("apagar")["meetings"])
        self.assertEqual(catalog.list_meetings(), [])

    def test_list_does_not_scan_ndjson(self):
        meeting = self._create(name="Com fala")
        meeting_id = meeting["id"]
        storage.append_ndjson(
            storage.meeting_path(meeting_id) / "transcript.ndjson",
            {"seq": 1, "speaker": "Rafael", "text": "olá"},
        )
        catalog.replace_transcript(meeting_id)
        rows = control.meeting_summaries()
        self.assertEqual(rows[0]["id"], meeting_id)
        self.assertEqual(rows[0]["segments"], 1)
        self.assertEqual(rows[0]["name"], "Com fala")


if __name__ == "__main__":
    unittest.main()
