"""Prompt do resumo: as notas de quem gravou e a citação da fala.

O que se garante aqui é o contrato que a tela usa: o modelo recebe o número da
fala (para poder citar) e as notas cruas (para poder marcar o que veio delas).
Sem isso, a lupa e o destaque viram decoração.
"""

import unittest

from fireball.summarizers.prompt import build_prompt


class PromptTest(unittest.TestCase):
    def test_transcricao_numerada_pede_citacao(self):
        texto = build_prompt({"name": "Reunião"})
        self.assertIn("`[12] Falante: texto`", texto)
        self.assertIn("vem numerada", texto.lower())

    def test_notas_entram_quando_existem(self):
        texto = build_prompt({"name": "Reunião"}, notes="3.4 travada no ts")
        self.assertIn("NOTAS DE QUEM GRAVOU", texto)
        self.assertIn("3.4 travada no ts", texto)
        self.assertIn("==assim==", texto)

    def test_sem_notas_nao_inventa_bloco(self):
        texto = build_prompt({"name": "Reunião"}, notes="   ")
        self.assertNotIn("NOTAS DE QUEM GRAVOU", texto)


class DialogoTest(unittest.TestCase):
    def test_dialogo_leva_o_numero_da_fala(self):
        from fireball.summary import as_dialogue

        texto = as_dialogue(
            [
                {"seq": 7, "speaker": "Ana", "text": "olho hoje"},
                {"seq": 8, "speaker": "Rafael", "text": ""},
            ]
        )
        self.assertEqual(texto, "[7] Ana: olho hoje")
