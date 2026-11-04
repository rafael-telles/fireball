"""Perguntar sobre a reunião: o contexto que vai no pedido e a recusa honesta.

Não se testa aqui a resposta do modelo (isso é rede); testa-se o que é nosso:
que o contexto sai com falante e horário, que o corte preserva a cauda, que o
histórico entra na ordem certa e que um provedor sem conversa é recusado com
mensagem que diz o que fazer.
"""

import unittest

from fireball import ask


class BuildContextTest(unittest.TestCase):
    def test_inclui_falante_e_horario(self):
        segments = [
            {"start": 0, "speaker": "Ivan", "text": "bom dia"},
            {"start": 75, "speaker": "Marina", "text": "olho hoje à tarde"},
        ]
        ctx = ask.build_context(segments)
        self.assertIn("[00:00] Ivan: bom dia", ctx)
        self.assertIn("[01:15] Marina: olho hoje à tarde", ctx)

    def test_sem_falante_nao_quebra(self):
        ctx = ask.build_context([{"start": 3600, "text": "fala solta"}])
        self.assertIn("[01:00:00] fala solta", ctx)

    def test_pula_fala_vazia(self):
        ctx = ask.build_context([{"start": 0, "speaker": "Ana", "text": "   "}])
        self.assertNotIn("Ana", ctx)

    def test_corte_preserva_a_cauda(self):
        segments = [
            {"start": i, "speaker": "Ana", "text": f"linha {i} " + "x" * 40}
            for i in range(400)
        ]
        ctx = ask.build_context(segments, limit=500)
        self.assertTrue(ctx.startswith("TRANSCRIÇÃO:\n…"))
        # a última fala é a que interessa numa pergunta sobre o fim da conversa
        self.assertIn("linha 399", ctx)
        self.assertNotIn("linha 0 ", ctx)

    def test_anexa_notas_e_resumo(self):
        ctx = ask.build_context(
            [{"start": 0, "speaker": "Ana", "text": "fala"}],
            notes="minha nota",
            summary_md="## Ata\nresumo",
        )
        self.assertIn("NOTAS DE QUEM GRAVOU:\nminha nota", ctx)
        self.assertIn("RESUMO JÁ GERADO:\n## Ata", ctx)

    def test_sem_notas_nem_resumo_nao_inventa_secao(self):
        ctx = ask.build_context([{"start": 0, "text": "fala"}])
        self.assertNotIn("NOTAS DE QUEM GRAVOU", ctx)
        self.assertNotIn("RESUMO JÁ GERADO", ctx)


class BuildMessagesTest(unittest.TestCase):
    def test_ordem_system_contexto_historico_pergunta(self):
        msgs = ask.build_messages(
            "e a release?",
            [{"role": "user", "content": "quem falou do ts?"},
             {"role": "assistant", "content": "o Rafael"}],
            "TRANSCRIÇÃO:\n[00:10] Rafael: o ts se perde",
        )
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "system")
        self.assertIn("TRANSCRIÇÃO", msgs[1]["content"])
        self.assertEqual(msgs[2], {"role": "user", "content": "quem falou do ts?"})
        self.assertEqual(msgs[3], {"role": "assistant", "content": "o Rafael"})
        self.assertEqual(msgs[-1], {"role": "user", "content": "e a release?"})

    def test_historico_longo_fica_com_as_ultimas_voltas(self):
        historico = [{"role": "user", "content": f"pergunta {i}"} for i in range(40)]
        msgs = ask.build_messages("agora", historico, "ctx")
        # system + system + 12 voltas + pergunta atual
        self.assertEqual(len(msgs), 2 + ask.MAX_HISTORY_TURNS + 1)
        self.assertIn("pergunta 39", msgs[-2]["content"])

    def test_volta_vazia_sai(self):
        msgs = ask.build_messages("agora", [{"role": "user", "content": "  "}], "ctx")
        self.assertEqual(len(msgs), 3)


class ProvedorTest(unittest.TestCase):
    def test_claude_code_recusa_explicando(self):
        with self.assertRaises(ask.AskUnavailable) as ctx:
            ask._provider_for_ask({"summary_provider": "claude_code"})
        self.assertIn("Claude Code", str(ctx.exception))

    def test_pergunta_vazia_recusa(self):
        with self.assertRaises(ask.AskUnavailable):
            ask.answer("qualquer", "   ")


class AcervoTest(unittest.TestCase):
    def test_termos_sem_palavras_de_pergunta(self):
        termos = ask.question_terms("O que decidimos com a Totvs sobre o MCP na reunião?")
        self.assertEqual(termos, ["totvs", "mcp"])

    def test_cada_termo_busca_sozinho_e_quem_casa_mais_sobe(self):
        indice = {
            "totvs": ["a", "b"],
            "mcp": ["b"],
        }

        def busca(termo, limit=20):
            return {"meetings": [
                {"id": mid, "started_at": "2026-11-01T10:00:00+00:00", "hits": []}
                for mid in indice.get(termo, [])
            ]}

        escolhidas = ask.pick_meetings("totvs e mcp", busca, lambda: [], limit=5)
        self.assertEqual([m["id"] for m in escolhidas], ["b", "a"])

    def test_recentes_completam_sem_repetir_e_sem_reuniao_vazia(self):
        def busca(termo, limit=20):
            return {"meetings": [{"id": "a", "started_at": None, "hits": []}]}

        recentes = [
            {"id": "a", "segments": 10},
            {"id": "vazia", "segments": 0},
            {"id": "c", "segments": 3},
        ]
        escolhidas = ask.pick_meetings("totvs", busca, lambda: recentes, limit=5)
        self.assertEqual([m["id"] for m in escolhidas], ["a", "c"])

    def test_reunioes_da_resposta_anterior_ficam_e_tudo_sai_por_data(self):
        def busca(termo, limit=20):
            return {"meetings": [
                {"id": f"busca{i}", "started_at": f"2026-10-0{i}T10:00:00+00:00", "hits": []}
                for i in range(1, 4)
            ]}

        recentes = [{"id": "antiga", "started_at": "2026-09-01T10:00:00+00:00", "segments": 5}]
        escolhidas = ask.pick_meetings(
            "pendente", busca, lambda: recentes, limit=3, pinned=["antiga"]
        )
        self.assertEqual([m["id"] for m in escolhidas], ["busca3", "busca2", "antiga"])

    def test_contexto_rotula_e_devolve_as_fontes(self):
        reunioes = [
            {"id": "x", "name": "Kickoff", "started_at": "2026-11-03T19:03:38+00:00",
             "tags": ["produto"], "hits": [{"speaker": "Ana", "snippet": "prazo em dezembro"}]},
            {"id": "y", "name": "", "started_at": None, "hits": []},
        ]
        ctx, fontes = ask.build_library_context(
            reunioes, lambda mid: "## Ata\nresumo" if mid == "x" else "", lambda mid: ""
        )
        self.assertIn("=== [R1] Kickoff", ctx)
        self.assertIn("tags: produto", ctx)
        self.assertIn("- Ana: prazo em dezembro", ctx)
        self.assertIn("[R2] Sem nome · data desconhecida", ctx)
        self.assertEqual([f["label"] for f in fontes], ["R1", "R2"])
        self.assertEqual(fontes[0]["id"], "x")

    def test_corte_mantem_ao_menos_uma_reuniao(self):
        reunioes = [{"id": str(i), "name": f"r{i}", "hits": []} for i in range(5)]
        _, fontes = ask.build_library_context(
            reunioes, lambda mid: "x" * 500, lambda mid: "", limit=100
        )
        self.assertEqual(len(fontes), 1)

    def test_pergunta_vazia_recusa(self):
        with self.assertRaises(ask.AskUnavailable):
            ask.answer_library("  ")


if __name__ == "__main__":
    unittest.main()
