from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import control, settings, storage
from fireball.daemon.core import DaemonCore


class TranscribeFinalTests(unittest.TestCase):
    """Desligar a transcrição final na configuração.

    Ela **reescreve** `transcript.ndjson` por cima, então quem desliga isso
    desliga para que nada reescreva sem pedir. A recusa mora no daemon, e não
    só no botão da janela: as skills rodam `fireball finalize` como parte do
    fluxo delas, e é justamente esse passo automático que a preferência precisa
    alcançar.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()
        self.core = DaemonCore()
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
        control.update_meeting(self.meeting_id, status="stopped")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_on_by_default(self):
        self.assertIs(settings.load()["transcribe_final"], True)

    def test_refuses_when_off(self):
        settings.save(transcribe_final=False)
        with self.assertRaises(control.FinalizeDisabled):
            self.core.finalize(self.meeting_id)

    def test_refuses_even_with_an_explicit_backend(self):
        """Desligada é desligada: passar `--backend` diz *qual* motor, não que
        a preferência não vale."""
        settings.save(transcribe_final=False)
        with self.assertRaises(control.FinalizeDisabled):
            self.core.finalize(self.meeting_id, backend="groq")

    def test_the_message_says_where_to_turn_it_back_on(self):
        settings.save(transcribe_final=False)
        try:
            self.core.finalize(self.meeting_id)
        except control.FinalizeDisabled as exc:
            self.assertIn("transcribe_final", str(exc))
            self.assertIn("Configurações", str(exc))
        else:  # pragma: no cover — o teste acima já garantiu que levanta
            self.fail("finalize deveria ter recusado")

    def test_refusing_does_not_touch_the_meeting(self):
        """A recusa acontece antes do lock e antes de qualquer escrita: uma
        reunião parada não pode ficar 'finalizing' para sempre por causa dela."""
        settings.save(transcribe_final=False)
        with self.assertRaises(control.FinalizeDisabled):
            self.core.finalize(self.meeting_id)

        self.assertEqual(control.read_meeting(self.meeting_id)["status"], "stopped")
        self.assertNotIn(self.meeting_id, self.core._finalizing)

    def test_turning_it_back_on_lets_the_job_start(self):
        settings.save(transcribe_final=False)
        with self.assertRaises(control.FinalizeDisabled):
            self.core.finalize(self.meeting_id)

        settings.save(transcribe_final=True)
        # `_supervise` também sai de cena: o que este teste mede é o portão do
        # `finalize`, não a thread que observa o filho morrer
        with patch.object(self.core, "_spawn") as spawn, patch.object(self.core, "_supervise"):
            spawn.return_value = _StubJob(self.meeting_id)
            self.core.finalize(self.meeting_id)

        kind = spawn.call_args.args[1]
        cmd = spawn.call_args.args[2]
        self.assertEqual(kind, "finalize")
        self.assertIn(self.meeting_id, cmd)
        self.assertEqual(control.read_meeting(self.meeting_id)["status"], "finalizing")

    def test_the_previous_result_is_cleared_only_when_it_actually_runs(self):
        """Recusar não pode apagar o resultado da finalização anterior: ela
        continua sendo a procedência do texto que está no disco."""
        storage.write_json(
            storage.meeting_path(self.meeting_id) / "finalize_result.json",
            {"segments": 10, "backend": "whisper", "replaced": True},
        )
        settings.save(transcribe_final=False)
        with self.assertRaises(control.FinalizeDisabled):
            self.core.finalize(self.meeting_id)

        self.assertEqual(control.transcript_meta(self.meeting_id)["backend"], "whisper")


class _StubJob:
    """O bastante de um `Job` para `finalize` seguir sem subir processo."""

    def __init__(self, meeting_id):
        self.meeting_id = meeting_id
        self.kind = "finalize"
        self.exit_code = None
        self.done = _Flag()


class _Flag:
    def wait(self, timeout=None):
        return True


if __name__ == "__main__":
    unittest.main()
