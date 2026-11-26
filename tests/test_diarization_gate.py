from __future__ import annotations

import unittest

import numpy as np

from fireball.diarization_gate import AlwaysOnStream, GatedDiarizationStream

RATE = 100  # amostras por segundo: contas redondas


class FakeInner:
    """Rotula na hora tudo que recebe; locutor muda a cada 10 s de áudio visto."""

    def __init__(self, lag_s: float = 0.0):
        self.fed = np.zeros(0, dtype=np.float32)
        self.lag_s = lag_s
        self.finished = False

    @property
    def labeled_until(self) -> float:
        return max(0.0, len(self.fed) / RATE - self.lag_s)

    def push(self, audio, _samplerate):
        self.fed = np.concatenate([self.fed, audio])

    def finish(self):
        self.finished = True
        self.lag_s = 0.0

    def turns(self, start, end):
        return [{"start": start, "end": end, "speaker_id": int(start // 10)}]


def chunk(seconds: float, value: float = 0.0):
    return np.full(int(seconds * RATE), value, dtype=np.float32)


class GateTests(unittest.TestCase):
    def make(self, inner=None):
        return GatedDiarizationStream(inner or FakeInner(), RATE, hangover_s=2.0, preroll_s=1.0)

    def test_silent_track_never_reaches_the_model(self):
        inner = FakeInner()
        gate = self.make(inner)
        for _ in range(10):
            gate.push(chunk(1), RATE, last_speech_end=None)
        self.assertEqual(len(inner.fed), 0)
        self.assertEqual(gate.labeled_until, 10.0)
        self.assertEqual(gate.turns(0, 10), [])
        gate.finish()
        self.assertFalse(inner.finished)

    def test_resume_sends_preroll_and_maps_turns_back_to_track_time(self):
        inner = FakeInner()
        gate = self.make(inner)
        for _ in range(30):  # 30 s de silêncio
            gate.push(chunk(1), RATE, last_speech_end=None)
        gate.push(chunk(1, 0.5), RATE, last_speech_end=31.0)  # voz em 30–31 s

        # 1 s de pre-roll (29–30) + o chunk novo
        self.assertEqual(len(inner.fed), 2 * RATE)
        self.assertEqual(inner.fed[0], 0.0)
        self.assertEqual(inner.fed[-1], 0.5)
        self.assertAlmostEqual(gate.labeled_until, 31.0)

        [turn] = gate.turns(30.2, 31.0)
        self.assertAlmostEqual(turn["start"], 30.2)
        self.assertAlmostEqual(turn["end"], 31.0)
        # no modelo essa fala está em 1,2 s: primeiro locutor
        self.assertEqual(turn["speaker_id"], 0)

    def test_pauses_only_after_hangover(self):
        inner = FakeInner()
        gate = self.make(inner)
        gate.push(chunk(1, 0.5), RATE, last_speech_end=1.0)
        gate.push(chunk(1), RATE, last_speech_end=1.0)  # 1 s depois da voz
        self.assertEqual(len(inner.fed), 2 * RATE)
        gate.push(chunk(1), RATE, last_speech_end=1.0)  # 2 s: pausa
        gate.push(chunk(5), RATE, last_speech_end=1.0)
        self.assertEqual(len(inner.fed), 2 * RATE)
        self.assertEqual(gate.labeled_until, 8.0)

    def test_does_not_pause_while_last_speech_is_unlabeled(self):
        inner = FakeInner(lag_s=4.0)  # rotula em blocos, atrasado
        gate = self.make(inner)
        gate.push(chunk(1, 0.5), RATE, last_speech_end=1.0)
        for _ in range(3):
            gate.push(chunk(1), RATE, last_speech_end=1.0)
        # 4 s depois da voz e o modelo ainda não rotulou até 1 s: segue alimentando
        self.assertEqual(len(inner.fed), 4 * RATE)
        gate.push(chunk(1), RATE, last_speech_end=1.0)
        self.assertEqual(len(inner.fed), 5 * RATE)  # com isso o modelo chega a 1 s
        gate.push(chunk(1), RATE, last_speech_end=1.0)
        self.assertEqual(len(inner.fed), 5 * RATE)  # pausou

    def test_turns_across_two_active_pieces(self):
        inner = FakeInner()
        gate = self.make(inner)
        gate.push(chunk(2, 0.5), RATE, last_speech_end=2.0)  # modelo 0–2
        for _ in range(20):
            gate.push(chunk(1), RATE, last_speech_end=2.0)  # 2–3 alimenta, depois pausa
        gate.push(chunk(1, 0.5), RATE, last_speech_end=23.0)  # volta em 22–23

        turns = gate.turns(0, 23)
        self.assertEqual([(round(t["start"], 2), round(t["end"], 2)) for t in turns], [(0, 3.0), (21.0, 23.0)])
        # o modelo viu 3 s, depois 1 s de pre-roll e o chunk: a 2ª parte está em 3–5 s nele
        self.assertEqual(len(inner.fed), 5 * RATE)

    def test_always_on_feeds_everything(self):
        inner = FakeInner()
        gate = AlwaysOnStream(inner, RATE)
        for _ in range(5):
            gate.push(chunk(1), RATE, last_speech_end=None)
        self.assertEqual(len(inner.fed), 5 * RATE)
        self.assertEqual(gate.turns(1, 2)[0]["start"], 1)


if __name__ == "__main__":
    unittest.main()
