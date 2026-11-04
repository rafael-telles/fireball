"""A nota da reunião: ela nasce vazia, e o cabeçalho de exemplo das pastas
antigas não passa por anotação de ninguém.

Por quê: o app escrevia `# <nome>` + "_Notas ao vivo tomadas pelo Claude e por
você._" + "## Notas" no notes.md ao criar a reunião. Na tela isso aparecia sob
"O que eu anotei" como se a pessoa tivesse escrito, duplicava o título do
documento e ia inteiro para o prompt do resumo.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fireball import control, storage

SEMENTE = (
    "# Notas da reunião\n\n"
    "_Notas ao vivo tomadas pelo Claude e por você._\n\n"
    "## Notas\n"
)


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _create(self, name=None):
        return control.create_meeting(
            name=name,
            fake=True,
            mic_device=None,
            system_device=None,
            transcribe=False,
            diarize=False,
            backend="whisper",
            language="pt",
            event=None,
        )

    def _notes_path(self, meeting):
        return storage.meeting_path(meeting["id"]) / "notes.md"

    def test_reuniao_nova_nasce_com_a_nota_vazia(self):
        meeting = self._create()
        self.assertEqual(self._notes_path(meeting).read_text(), "")
        self.assertEqual(control.read_notes(meeting["id"]), "")

    def test_reuniao_com_nome_tambem_nasce_sem_texto(self):
        meeting = self._create(name="1:1 com Ana")
        self.assertEqual(control.read_notes(meeting["id"]), "")

    def test_semente_antiga_le_como_nota_vazia(self):
        meeting = self._create()
        self._notes_path(meeting).write_text(SEMENTE)
        self.assertEqual(control.read_notes(meeting["id"]), "")

    def test_semente_com_o_nome_da_reuniao_tambem_e_vazia(self):
        meeting = self._create(name="Planejamento Q1")
        self._notes_path(meeting).write_text(
            SEMENTE.replace("Notas da reunião", "Planejamento Q1")
        )
        self.assertEqual(control.read_notes(meeting["id"]), "")

    def test_anotacao_de_verdade_volta_inteira(self):
        meeting = self._create()
        texto = "# Orçamento\n\n- Falar do desconto\n- Ver o contrato\n"
        control.write_notes(meeting["id"], texto)
        self.assertEqual(control.read_notes(meeting["id"]), texto)

    def test_titulo_escrito_a_mao_nao_vira_nota_vazia(self):
        meeting = self._create()
        control.write_notes(meeting["id"], "# Pauta\n")
        self.assertEqual(control.read_notes(meeting["id"]), "# Pauta\n")

    def test_semente_com_anotacao_embaixo_nao_e_descartada(self):
        meeting = self._create()
        texto = SEMENTE + "\n- desconto de 12%\n"
        self._notes_path(meeting).write_text(texto)
        self.assertEqual(control.read_notes(meeting["id"]), texto)

    def test_deteccao_da_semente(self):
        self.assertTrue(storage.is_note_seed(SEMENTE))
        self.assertFalse(storage.is_note_seed(""))
        self.assertFalse(storage.is_note_seed("# Orçamento\n\n- item\n"))
        self.assertFalse(storage.is_note_seed("texto solto"))


if __name__ == "__main__":
    unittest.main()
