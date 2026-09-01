from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from fireball import control, storage, voices


class FakeEmbeddingBackend:
    name = "fake"
    model = "test-model"

    def __init__(self, embedding=None):
        self.embedding = np.asarray(embedding or [1.0, 0.0, 0.0], dtype=np.float32)

    def embed(self, _audio, samplerate=16000):
        return self.embedding


def write_wav(path: Path, seconds: float = 8.0) -> None:
    audio = (np.sin(np.arange(int(16000 * seconds)) * 0.03) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(audio.tobytes())


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_profile_is_private_and_contains_no_audio(self):
        profile = voices.save_profile(
            "Ana",
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            model="test-model",
            email="ANA@example.com",
        )
        path = voices.profiles_root() / f"{profile['id']}.json"
        raw = json.loads(path.read_text())

        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(voices.profiles_root().stat().st_mode & 0o777, 0o700)
        self.assertEqual(raw["emails"], ["ana@example.com"])
        self.assertNotIn("audio", raw)
        self.assertNotIn("embeddings", profile)
        self.assertNotIn("centroid", profile)

    def test_rename_replaces_emails(self):
        profile = voices.save_profile(
            "Ana",
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            model="test-model",
            email="ana@example.com",
        )
        updated = voices.rename_profile(
            profile["id"],
            "Ana Costa",
            emails=["ANA@example.com", "ana.costa@empresa.com"],
        )
        self.assertEqual(updated["name"], "Ana Costa")
        self.assertEqual(updated["emails"], ["ana.costa@empresa.com", "ana@example.com"])
        cleared = voices.rename_profile(profile["id"], "Ana Costa", emails=[])
        self.assertEqual(cleared["emails"], [])
        same = voices.rename_profile(profile["id"], "Ana")
        self.assertEqual(same["emails"], [])

    def test_match_requires_threshold_and_margin(self):
        ana = voices.save_profile(
            "Ana", np.array([1.0, 0.0, 0.0]), model="test-model"
        )
        voices.save_profile(
            "Bia", np.array([0.0, 1.0, 0.0]), model="test-model"
        )
        matcher = voices.VoiceMatcher(FakeEmbeddingBackend([1.0, 0.01, 0.0]))
        match = matcher.identify(np.ones(16000, dtype=np.float32))
        self.assertEqual(match["profile_id"], ana["id"])

        scaled = voices.VoiceMatcher(FakeEmbeddingBackend([10.0, 0.1, 0.0]))
        self.assertEqual(
            scaled.identify(np.ones(16000, dtype=np.float32))["profile_id"],
            ana["id"],
        )

        voices.delete_profile(ana["id"])
        voices.save_profile(
            "Ana 2", np.array([0.70, 0.70, 0.0]), model="test-model"
        )
        voices.save_profile(
            "Bia 2", np.array([0.69, 0.71, 0.0]), model="test-model"
        )
        ambiguous = voices.VoiceMatcher(FakeEmbeddingBackend([0.70, 0.70, 0.0]))
        self.assertIsNone(ambiguous.identify(np.ones(16000, dtype=np.float32)))

    def test_same_email_does_not_silently_merge_profiles(self):
        first = voices.save_profile(
            "Ana", np.array([1.0, 0.0]), model="test-model", email="shared@example.com"
        )
        second = voices.save_profile(
            "Bia", np.array([0.0, 1.0]), model="test-model", email="shared@example.com"
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(voices.public_profiles()), 2)

    def test_manual_label_survives_finalize_without_profiles(self):
        meeting = Path(self.temp.name) / "meeting"
        meeting.mkdir()
        assignment = {
            "profile_id": "deleted-profile",
            "name": "Ana",
            "source": "manual",
            "confidence": 1.0,
        }
        voices.save_label(meeting, "system:0", assignment)
        labels = voices.identify_diarized_tracks(
            meeting,
            {"system": [{"start": 0.0, "end": 1.0, "speaker_id": 0}]},
        )
        self.assertEqual(labels["system:0"]["name"], "Ana")

    def test_live_matching_is_unique_per_track_and_caps_audio(self):
        class NeverMatcher:
            profiles = [{}]

            def identify(self, _audio, _samplerate):
                return None

        recognizer = voices.LiveVoiceRecognizer.__new__(voices.LiveVoiceRecognizer)
        recognizer.meeting_dir = Path(self.temp.name)
        recognizer._backend = None
        recognizer.matcher = NeverMatcher()
        recognizer.buffers = {}
        recognizer.samples = {}
        recognizer.next_attempt = {}
        recognizer.matches = {}
        recognizer.has_profiles = True
        recognizer._labels_stamp = voices._stamp(voices.labels_path(recognizer.meeting_dir))
        recognizer._profiles_stamp = voices._stamp(voices.profiles_root())
        chunk = np.ones(10 * 16000, dtype=np.float32)
        for _ in range(8):
            recognizer.observe("mic", 0, chunk, 16000)
        self.assertLessEqual(
            sum(len(part) for part in recognizer.buffers["mic:0"]),
            int(voices.MAX_SAMPLE_SECONDS * 16000),
        )

        class SameMatcher:
            profiles = [{}]

            def identify(self, _audio, _samplerate):
                return {
                    "profile_id": "ana",
                    "name": "Ana",
                    "source": "voice",
                    "confidence": 0.9,
                }

        recognizer.matcher = SameMatcher()
        recognizer.buffers = {}
        recognizer.samples = {}
        recognizer.next_attempt = {}
        self.assertIsNotNone(recognizer.observe("system", 0, chunk, 16000))
        self.assertIsNone(recognizer.observe("system", 1, chunk, 16000))

    @patch("fireball.voices.get_speaker_embedding_backend", return_value=FakeEmbeddingBackend())
    def test_enroll_from_finalized_meeting_assigns_every_segment(self, _backend):
        meeting = Path(self.temp.name) / "meetings" / "meeting-1"
        meeting.mkdir(parents=True)
        write_wav(meeting / "mic.wav")
        storage.write_json(
            meeting / "diarization.json",
            {"mic": [{"start": 0.0, "end": 8.0, "speaker_id": 0}]},
        )
        storage.append_ndjson(
            meeting / "transcript.ndjson",
            {"seq": 1, "speaker": "Sala 1", "text": "oi", "start": 0.0, "end": 2.0},
        )

        result = voices.enroll_from_meeting(meeting, "mic", 0, "Rafael")
        control.assign_speaker("meeting-1", "mic:0", result["assignment"])
        transcript = list(storage.read_ndjson(meeting / "transcript.ndjson"))

        self.assertEqual(transcript[0]["speaker"], "Rafael")
        self.assertEqual(transcript[0]["speaker_key"], "mic:0")
        self.assertEqual(transcript[0]["voice_profile_id"], result["profile"]["id"])
        self.assertEqual(
            voices.load_labels(meeting)["mic:0"]["source"],
            "manual",
        )

    @patch("fireball.voices.get_speaker_embedding_backend", return_value=FakeEmbeddingBackend())
    def test_enroll_during_recording_uses_pcm_and_live_turns(self, _backend):
        """Sem diarization.json e sem .wav — só o que a reunião viva tem."""
        meeting = Path(self.temp.name) / "meetings" / "meeting-live"
        meeting.mkdir(parents=True)
        pcm = (np.sin(np.arange(16000 * 8) * 0.03) * 12000).astype(np.int16)
        (meeting / "mic.pcm").write_bytes(pcm.tobytes())
        for seq, (start, end) in enumerate([(0.0, 3.0), (3.0, 8.0)], start=1):
            storage.append_ndjson(
                meeting / "transcript.ndjson",
                {
                    "seq": seq,
                    "speaker": "Sala 1",
                    "text": "oi",
                    "start": start,
                    "end": end,
                    "track": "mic",
                    "speaker_id": 0,
                    "speaker_key": "mic:0",
                },
            )

        result = voices.enroll_from_meeting(meeting, "mic", 0, "Rafael")

        # gravando, a transcrição no disco não é tocada; o nome vem na leitura
        raw = list(storage.read_ndjson(meeting / "transcript.ndjson"))
        self.assertEqual([seg["speaker"] for seg in raw], ["Sala 1", "Sala 1"])
        read = control.read_transcript("meeting-live")
        self.assertEqual([seg["speaker"] for seg in read], ["Rafael", "Rafael"])
        self.assertEqual(read[0]["voice_profile_id"], result["profile"]["id"])

        # e passa para o arquivo quando o engine sai
        self.assertEqual(control.apply_speaker_labels("meeting-live"), 2)
        persisted = list(storage.read_ndjson(meeting / "transcript.ndjson"))
        self.assertEqual([seg["speaker"] for seg in persisted], ["Rafael", "Rafael"])
        self.assertEqual(control.apply_speaker_labels("meeting-live"), 0)

    @patch("fireball.voices.get_speaker_embedding_backend", return_value=FakeEmbeddingBackend())
    def test_live_recognizer_picks_up_names_given_mid_meeting(self, _backend):
        meeting = Path(self.temp.name) / "meetings" / "meeting-live"
        meeting.mkdir(parents=True)
        recognizer = voices.LiveVoiceRecognizer(meeting)
        chunk = np.ones(6 * 16000, dtype=np.float32)

        self.assertFalse(recognizer.available)
        self.assertIsNone(recognizer.observe("mic", 0, chunk, 16000))
        self.assertEqual(recognizer.buffers, {})

        assignment = {
            "profile_id": "ana",
            "name": "Ana",
            "source": "manual",
            "confidence": 1.0,
        }
        voices.save_label(meeting, "mic:0", assignment)
        self.assertEqual(recognizer.observe("mic", 0, chunk, 16000), assignment)


if __name__ == "__main__":
    unittest.main()
