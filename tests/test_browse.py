from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import catalog, control, storage


class BrowseTests(unittest.TestCase):
    """A varredura do acervo: filtros, ordem e agregados numa consulta só."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _meeting(self, name, started, ended, tags=(), event=None):
        meeting = control.create_meeting(
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
        control.write_meeting_fields(
            meeting["id"],
            started_at=started,
            ended_at=ended,
            status="finalized",
            tags=list(tags),
        )
        return meeting["id"]

    def _fixtures(self):
        self.a = self._meeting(
            "Planejamento do trimestre",
            "2026-08-30T14:02:00",
            "2026-08-30T14:50:00",  # 48 min
            tags=["produto", "q4"],
        )
        self.b = self._meeting(
            "1:1 com a Marina",
            "2026-08-27T09:30:00",
            "2026-08-27T09:56:00",  # 26 min
            tags=["1:1"],
            event={"attendees": [{"name": "Marina Alves", "email": "marina@vertico.com.br"}]},
        )
        self.c = self._meeting(
            "Retro da sprint 34",
            "2026-06-01T17:10:00",
            "2026-06-01T18:01:00",  # 51 min
            tags=["retro", "produto"],
        )

    def test_totals_come_with_the_rows(self):
        self._fixtures()
        result = catalog.browse()
        self.assertEqual(result["totals"]["meetings"], 3)
        self.assertEqual(result["totals"]["duration_s"], (48 + 26 + 51) * 60)

    def test_filters_by_tag_and_by_person(self):
        self._fixtures()
        by_tag = catalog.browse(tags=["produto"])
        self.assertEqual({row["id"] for row in by_tag["meetings"]}, {self.a, self.c})
        self.assertEqual(by_tag["totals"]["meetings"], 2)

        by_person = catalog.browse(people=["Marina Alves"])
        self.assertEqual([row["id"] for row in by_person["meetings"]], [self.b])

        # e-mail vale como pessoa: é o que o chip guarda quando não há nome
        by_email = catalog.browse(people=["marina@vertico.com.br"])
        self.assertEqual([row["id"] for row in by_email["meetings"]], [self.b])

    def test_tag_filter_is_any_of_the_chosen(self):
        self._fixtures()
        result = catalog.browse(tags=["1:1", "retro"])
        self.assertEqual({row["id"] for row in result["meetings"]}, {self.b, self.c})

    def test_filters_stack(self):
        self._fixtures()
        result = catalog.browse(tags=["produto"], since="2026-08-01")
        self.assertEqual([row["id"] for row in result["meetings"]], [self.a])

    def test_date_range_cuts_both_ends(self):
        self._fixtures()
        self.assertEqual(
            {row["id"] for row in catalog.browse(since="2026-08-01")["meetings"]},
            {self.a, self.b},
        )
        self.assertEqual(
            [row["id"] for row in catalog.browse(until="2026-08-28")["meetings"]],
            [self.b, self.c],  # mais recentes primeiro
        )

    def test_search_text_reaches_the_transcript(self):
        self._fixtures()
        storage.append_ndjson(
            storage.meeting_path(self.c) / "transcript.ndjson",
            {"seq": 1, "speaker": "Rafael", "text": "o corte de escopo do onboarding travou"},
        )
        catalog.replace_transcript(self.c)

        self.assertEqual([row["id"] for row in catalog.browse(query="onboarding")["meetings"]], [self.c])
        self.assertEqual([row["id"] for row in catalog.browse(query="Marina")["meetings"]], [self.b])
        self.assertEqual(catalog.browse(query="assunto-que-nao-existe")["meetings"], [])

    def test_sorts_by_name_and_by_duration(self):
        self._fixtures()
        by_name = catalog.browse(sort="name", desc=False)
        self.assertEqual([row["name"] for row in by_name["meetings"]][0], "1:1 com a Marina")

        by_duration = catalog.browse(sort="duration", desc=True)
        self.assertEqual([row["id"] for row in by_duration["meetings"]], [self.c, self.a, self.b])

    def test_unknown_sort_falls_back_instead_of_reaching_the_sql(self):
        self._fixtures()
        result = catalog.browse(sort="'; DROP TABLE meetings; --")
        self.assertEqual(result["totals"]["meetings"], 3)

    def test_meeting_without_duration_sorts_last_either_way(self):
        self._fixtures()
        live = self._meeting("Gravando agora", "2026-09-01T10:00:00", None)
        control.write_meeting_fields(live, status="recording")

        for desc in (True, False):
            order = [row["id"] for row in catalog.browse(sort="duration", desc=desc)["meetings"]]
            self.assertEqual(order[-1], live, f"desc={desc}")

    def test_row_carries_what_the_table_draws(self):
        self._fixtures()
        (storage.meeting_path(self.a) / "summary.md").write_text(
            "# Planejamento\n\nMetas de Q4 e corte de escopo do onboarding.\n"
        )
        catalog.index_card(self.a)

        row = next(r for r in catalog.browse()["meetings"] if r["id"] == self.a)
        self.assertEqual(row["subtitle"], "Metas de Q4 e corte de escopo do onboarding.")
        self.assertEqual(row["tags"], ["produto", "q4"])
        self.assertEqual(row["duration_s"], 48 * 60)

    def test_people_of_a_row_mix_attendees_and_speakers(self):
        self._fixtures()
        storage.append_ndjson(
            storage.meeting_path(self.b) / "transcript.ndjson",
            {"seq": 1, "speaker": "João Pedro", "text": "olá"},
        )
        catalog.replace_transcript(self.b)

        row = next(r for r in catalog.browse()["meetings"] if r["id"] == self.b)
        self.assertEqual(
            {person["label"] for person in row["people"]}, {"Marina Alves", "João Pedro"}
        )

    def test_a_recognized_voice_and_its_attendee_are_one_face(self):
        """A voz reconhecida e o convidado da agenda são a mesma pessoa mesmo
        quando os dois nomes não batem letra por letra: o e-mail do perfil de
        voz é o que os liga, e sem ele a pilha de avatares mostrava duas."""
        import numpy as np

        from fireball import voices

        profile = voices.save_profile(
            "Marina Alves",
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            model="test-model",
            email="marina@vertico.com.br",
        )
        meeting_id = self._meeting(
            "Retro",
            "2026-08-20T10:00:00",
            "2026-08-20T10:30:00",
            event={"attendees": [{"name": "Marina A. Alves", "email": "marina@vertico.com.br"}]},
        )
        storage.append_ndjson(
            storage.meeting_path(meeting_id) / "transcript.ndjson",
            {
                "seq": 1,
                "speaker": "Marina Alves",
                "voice_profile_id": profile["id"],
                "text": "olá",
            },
        )
        catalog.replace_transcript(meeting_id)

        row = next(r for r in catalog.browse()["meetings"] if r["id"] == meeting_id)
        self.assertEqual([person["label"] for person in row["people"]], ["Marina A. Alves"])

    def test_the_same_name_from_both_sources_is_one_face(self):
        meeting_id = self._meeting(
            "Sync",
            "2026-08-21T10:00:00",
            "2026-08-21T10:30:00",
            event={"attendees": [{"name": "Marina Alves", "email": "marina@vertico.com.br"}]},
        )
        storage.append_ndjson(
            storage.meeting_path(meeting_id) / "transcript.ndjson",
            {"seq": 1, "speaker": "Marina Alves", "text": "olá"},
        )
        catalog.replace_transcript(meeting_id)

        row = next(r for r in catalog.browse()["meetings"] if r["id"] == meeting_id)
        self.assertEqual([person["label"] for person in row["people"]], ["Marina Alves"])
        # o do convite vem primeiro: é o que tem e-mail
        self.assertEqual(row["people"][0]["source"], "attendee")

    def test_facets_count_the_whole_library(self):
        self._fixtures()
        facets = catalog.facets()

        self.assertEqual(facets["meetings"], 3)
        self.assertEqual(facets["duration_s"], (48 + 26 + 51) * 60)
        # a tag mais usada primeiro: é ela que serve para escolher
        self.assertEqual(facets["tags"][0], {"tag": "produto", "meetings": 2})
        self.assertEqual(
            [row["label"] for row in facets["people"]], ["Marina Alves"]
        )

    def test_deleting_a_meeting_drops_its_tags_from_the_facets(self):
        self._fixtures()
        control.delete_meeting(self.b)
        facets = catalog.facets()

        self.assertEqual(facets["meetings"], 2)
        self.assertNotIn("1:1", [row["tag"] for row in facets["tags"]])
        self.assertEqual(facets["people"], [])

    def test_set_tags_cleans_and_reindexes(self):
        self._fixtures()
        control.set_tags(self.a, ["  produto ", "produto", "", "novo"])
        self.assertEqual(control.read_meeting(self.a)["tags"], ["produto", "novo"])
        self.assertEqual({row["id"] for row in catalog.browse(tags=["novo"])["meetings"]}, {self.a})


if __name__ == "__main__":
    unittest.main()
