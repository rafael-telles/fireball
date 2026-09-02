from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import control, stats, storage


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _meeting(self, started="2026-08-30T14:00:00", ended="2026-08-30T14:10:00"):
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
        control.write_meeting_fields(
            meeting["id"], started_at=started, ended_at=ended, status="finalized"
        )
        return meeting["id"]

    def _write(self, meeting_id, segments):
        path = storage.meeting_path(meeting_id) / "transcript.ndjson"
        for seg in segments:
            storage.append_ndjson(path, seg)

    def test_silence_is_the_gaps_plus_the_tail(self):
        meeting_id = self._meeting()  # 600 s gravados
        self._write(
            meeting_id,
            [
                {"seq": 1, "start": 0, "end": 60, "speaker": "Você", "text": "um dois três"},
                {"seq": 2, "start": 120, "end": 180, "speaker": "Marina", "text": "quatro cinco"},
            ],
        )
        result = control.meeting_stats(meeting_id)

        self.assertTrue(result["timed"])
        self.assertEqual(result["recorded_seconds"], 600)
        self.assertEqual(result["speech_seconds"], 120)
        # 60 s entre as falas + 420 s depois da última
        self.assertEqual(result["silence_seconds"], 480)
        self.assertEqual(result["longest_silence"]["seconds"], 420)
        self.assertEqual(result["turn_changes"], 1)

    def test_overlapping_speech_counts_once_as_silence_but_twice_per_person(self):
        meeting_id = self._meeting(ended="2026-08-30T14:02:00")  # 120 s
        self._write(
            meeting_id,
            [
                {"seq": 1, "start": 0, "end": 60, "speaker": "Você", "text": "a"},
                {"seq": 2, "start": 30, "end": 90, "speaker": "Marina", "text": "b"},
            ],
        )
        result = control.meeting_stats(meeting_id)

        # a união dos dois trechos é 0–90, não 120 s de fala
        self.assertEqual(result["speech_seconds"], 90)
        self.assertEqual(result["silence_seconds"], 30)
        # por pessoa, quem falou por cima falou aquele tempo
        self.assertEqual({s["speaker"]: s["seconds"] for s in result["speakers"]}, {"Você": 60.0, "Marina": 60.0})

    def test_same_speaker_twice_is_not_a_turn_change(self):
        meeting_id = self._meeting()
        self._write(
            meeting_id,
            [
                {"seq": 1, "start": 0, "end": 10, "speaker": "Você", "text": "a"},
                {"seq": 2, "start": 20, "end": 30, "speaker": "Você", "text": "b"},
                {"seq": 3, "start": 40, "end": 50, "speaker": "Marina", "text": "c"},
            ],
        )
        self.assertEqual(control.meeting_stats(meeting_id)["turn_changes"], 1)

    def test_live_transcript_has_no_silence_to_report(self):
        """A do tempo real carimba hora de relógio, não duração — e um silêncio
        inventado de zero segundo seria pior que dizer que não dá."""
        meeting_id = self._meeting()
        self._write(
            meeting_id,
            [
                {"seq": 1, "ts": "2026-08-30T14:01:00", "speaker": "Você", "text": "uma fala"},
                {"seq": 2, "ts": "2026-08-30T14:03:00", "speaker": "Marina", "text": "outra fala"},
            ],
        )
        result = control.meeting_stats(meeting_id)

        self.assertFalse(result["timed"])
        self.assertIsNone(result["silence_seconds"])
        self.assertEqual(result["speakers"], [])
        self.assertEqual(result["recorded_seconds"], 600)
        self.assertEqual(result["words"], 4)

    def test_speaker_key_comes_along_for_the_voice_dialog(self):
        meeting_id = self._meeting()
        self._write(
            meeting_id,
            [{"seq": 1, "start": 0, "end": 10, "speaker": "Sala 2", "speaker_key": "mic:1", "text": "a"}],
        )
        speaker = control.meeting_stats(meeting_id)["speakers"][0]
        self.assertEqual(speaker["speaker_key"], "mic:1")

    def test_timeline_has_a_fixed_number_of_buckets(self):
        meeting_id = self._meeting()
        self._write(meeting_id, [{"seq": 1, "start": 0, "end": 300, "speaker": "Você", "text": "a"}])
        timeline = control.meeting_stats(meeting_id)["timeline"]

        self.assertEqual(len(timeline), stats.TIMELINE_BUCKETS)
        # metade da reunião foi fala: a primeira metade das barras está cheia
        self.assertEqual(timeline[0]["ratio"], 1.0)
        self.assertEqual(timeline[-1]["ratio"], 0.0)

    def test_meeting_that_never_closed_falls_back_to_the_transcript(self):
        meeting_id = self._meeting(ended=None)
        self._write(meeting_id, [{"seq": 1, "start": 0, "end": 42, "speaker": "Você", "text": "a"}])
        self.assertEqual(control.meeting_stats(meeting_id)["recorded_seconds"], 42)


if __name__ == "__main__":
    unittest.main()
