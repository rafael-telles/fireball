from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from fireball import control, finalize, realtime, storage, vad
from fireball.backends import BackendUnavailable
from fireball.backends.sortformer_backend import parse_rttm


def write_silence(path: Path, seconds: float = 2.0) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(np.zeros(int(16000 * seconds), dtype=np.int16).tobytes())


class FakeAsr:
    def transcribe_file(self, _path, _language):
        return [{"start": None, "end": None, "text": "fala"}]


class FakeDiarizer:
    def diarize_files(self, tracks):
        return {
            key: [{"start": 0.25, "end": 1.25, "speaker_id": 0}]
            for key in tracks
        }


class FakeRealtimeAsr:
    def transcribe_chunk(self, _audio, _samplerate, _language):
        return "fala"


class FakeDiarizationStream:
    labeled_until = 0.0

    def push(self, _audio, _samplerate):
        pass

    def finish(self):
        self.labeled_until = 10.0

    def turns(self, start, end):
        middle = (start + end) / 2
        return [
            {"start": start, "end": middle, "speaker_id": 0},
            {"start": middle, "end": end, "speaker_id": 1},
        ]


class FakeStreamingDiarizer:
    def __init__(self):
        self.closed = False

    def open_stream(self):
        return FakeDiarizationStream()

    def close(self):
        self.closed = True


class FakeVoiceRecognizer:
    def observe(self, track, speaker_id, _audio, _samplerate):
        if track == "system" and speaker_id == 0:
            return {
                "profile_id": "ana",
                "name": "Ana",
                "source": "voice",
                "confidence": 0.91,
            }
        return None


class DiarizationTests(unittest.TestCase):
    def test_engine_command_propagates_live_diarization(self):
        command = control.engine_command(
            "meeting-id",
            fake=False,
            interval=3.0,
            mic_device=None,
            system_device=None,
            transcribe=True,
            diarize=True,
            backend="parakeet",
            language="pt",
        )
        self.assertIn("--diarize", command)

    def test_parse_rttm_assigns_arrival_ids_and_removes_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "track.rttm"
            path.write_text(
                "\n".join(
                    [
                        "SPEAKER track 1 0.00 1.00 <NA> <NA> speaker_A <NA> <NA>",
                        "SPEAKER track 1 0.95 1.00 <NA> <NA> speaker_B <NA> <NA>",
                        "SPEAKER track 1 2.00 0.50 <NA> <NA> speaker_A <NA> <NA>",
                    ]
                )
            )

            self.assertEqual(
                parse_rttm(path),
                [
                    {"start": 0.0, "end": 1.0, "speaker_id": 0},
                    {"start": 1.0, "end": 1.95, "speaker_id": 1},
                    {"start": 2.0, "end": 2.5, "speaker_id": 0},
                ],
            )

    def _meeting(self, root: Path, diarize: bool = True) -> None:
        storage.write_json(
            root / "meeting.json",
            {"fake": False, "backend": "parakeet", "language": "pt", "diarize": diarize},
        )
        storage.write_json(root / "audio_tracks.json", {"mic": {}, "system": {}})
        (root / "transcript.ndjson").touch()
        write_silence(root / "mic.wav")
        write_silence(root / "system.wav")

    @patch("fireball.control.storage.now_iso", return_value="2026-09-01T12:00:00")
    @patch("fireball.control.storage.new_meeting_dir")
    def test_real_meeting_persists_diarization_choice(self, new_meeting_dir, _now):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_meeting_dir.return_value = root

            meeting = control.create_meeting(
                name=None,
                fake=False,
                mic_device=None,
                system_device=None,
                transcribe=True,
                diarize=True,
                backend="parakeet",
                language="pt",
            )

            self.assertTrue(meeting["diarize"])
            self.assertTrue(storage.read_json(root / "meeting.json")["diarize"])

    @patch("fireball.finalize.get_diarization_backend", return_value=FakeDiarizer())
    @patch("fireball.finalize.get_batch_backend", return_value=FakeAsr())
    @patch("fireball.finalize.voices.identify_diarized_tracks", return_value={})
    def test_finalize_diarizes_room_and_remote_separately(self, _voices, _asr, _diarizer):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._meeting(root)

            result = finalize.run_finalize(root)
            transcript = list(storage.read_ndjson(root / "transcript.ndjson"))

            self.assertTrue(result["diarized"])
            self.assertEqual([item["speaker"] for item in transcript], ["Sala 1", "Remoto 1"])
            self.assertTrue((root / "diarization.json").exists())

    @patch("fireball.finalize.get_diarization_backend", return_value=FakeDiarizer())
    @patch("fireball.finalize.get_batch_backend", return_value=FakeAsr())
    @patch(
        "fireball.finalize.voices.identify_diarized_tracks",
        return_value={
            "mic:0": {
                "profile_id": "rafael",
                "name": "Rafael",
                "source": "voice",
                "confidence": 0.93,
            }
        },
    )
    def test_finalize_writes_recognized_identity(self, _voices, _asr, _diarizer):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._meeting(root)

            finalize.run_finalize(root)
            transcript = list(storage.read_ndjson(root / "transcript.ndjson"))
            local = next(item for item in transcript if item["track"] == "mic")

            self.assertEqual(local["speaker"], "Rafael")
            self.assertEqual(local["speaker_key"], "mic:0")
            self.assertEqual(local["voice_profile_id"], "rafael")
            self.assertEqual(local["speaker_confidence"], 0.93)

    @patch(
        "fireball.finalize.get_diarization_backend",
        side_effect=BackendUnavailable("runtime ausente"),
    )
    @patch("fireball.finalize.get_batch_backend", return_value=FakeAsr())
    @patch("fireball.finalize._transcribe_track")
    def test_finalize_falls_back_when_sortformer_is_unavailable(self, transcribe, _asr, _diarizer):
        transcribe.return_value = [{"start": 0.0, "end": 1.0, "text": "fala"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._meeting(root)

            result = finalize.run_finalize(root)
            transcript = list(storage.read_ndjson(root / "transcript.ndjson"))

            self.assertFalse(result["diarized"])
            self.assertEqual([item["speaker"] for item in transcript], ["Você", "Outros participantes"])
            self.assertIn("runtime ausente", (root / "diarization_warnings.log").read_text())

    def test_realtime_splits_each_track_into_diarized_turns(self):
        class FakeEndpointer:
            def __init__(self, samplerate):
                self.samplerate = samplerate

            def push(self, audio):
                return [vad.Utterance(audio=audio, start=0.0, end=len(audio) / self.samplerate)]

            def flush(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples = np.ones(16000 * 2, dtype=np.int16)
            (root / "mic.pcm").write_bytes(samples.tobytes())
            (root / "system.pcm").write_bytes(samples.tobytes())
            diarizer = FakeStreamingDiarizer()

            with patch("fireball.realtime.Endpointer", FakeEndpointer):
                realtime.run_realtime_transcription(
                    root,
                    stop_flag=lambda: True,
                    backend=FakeRealtimeAsr(),
                    diarization_backend=diarizer,
                )

            transcript = list(storage.read_ndjson(root / "transcript.ndjson"))
            self.assertEqual(
                [item["speaker"] for item in transcript],
                ["Sala 1", "Sala 2", "Remoto 1", "Remoto 2"],
            )
            self.assertTrue(diarizer.closed)

    def test_realtime_uses_recognized_name_and_keeps_speaker_key(self):
        class FakeEndpointer:
            def __init__(self, samplerate):
                self.samplerate = samplerate

            def push(self, audio):
                return [vad.Utterance(audio=audio, start=0.0, end=len(audio) / self.samplerate)]

            def flush(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples = np.ones(16000 * 2, dtype=np.int16)
            (root / "mic.pcm").write_bytes(samples.tobytes())
            (root / "system.pcm").write_bytes(samples.tobytes())

            with patch("fireball.realtime.Endpointer", FakeEndpointer):
                realtime.run_realtime_transcription(
                    root,
                    stop_flag=lambda: True,
                    backend=FakeRealtimeAsr(),
                    diarization_backend=FakeStreamingDiarizer(),
                    voice_recognizer=FakeVoiceRecognizer(),
                )

            transcript = list(storage.read_ndjson(root / "transcript.ndjson"))
            recognized = next(item for item in transcript if item["speaker"] == "Ana")
            self.assertEqual(recognized["speaker_key"], "system:0")
            self.assertEqual(recognized["voice_profile_id"], "ana")
            self.assertEqual(recognized["speaker_confidence"], 0.91)


if __name__ == "__main__":
    unittest.main()
