from __future__ import annotations

import unittest

from fireball.gui import pagina


def _explode() -> None:
    raise RuntimeError("janela já foi embora")


class PaginaDaJanelaTests(unittest.TestCase):
    """A regra, sem Qt e sem janela: quem carrega o HTML é injetado."""

    def setUp(self):
        self.carregados: list[str] = []
        self.logs: list[str] = []
        self.pagina = pagina.PaginaDaJanela(
            carregar_app=lambda: self.carregados.append(pagina.APP),
            carregar_ociosa=lambda: self.carregados.append(pagina.OCIOSA),
            log=self.logs.append,
        )

    def test_nasce_ocioso_sem_carregar_nada(self):
        self.assertEqual(self.pagina.estado, pagina.OCIOSA)
        self.assertEqual(self.carregados, [])

    def test_estado_inicial_app_nao_se_disfarca_de_ocioso(self):
        """Quem cria a janela já com a página do app diz isso — senão o
        primeiro `ao_esconder()` vira no-op e a página fica carregada."""
        self.pagina = pagina.PaginaDaJanela(
            lambda: self.carregados.append(pagina.APP),
            lambda: self.carregados.append(pagina.OCIOSA),
            estado_inicial=pagina.APP,
            log=self.logs.append,
        )
        self.assertEqual(self.pagina.ao_mostrar(), pagina.NADA)     # já estava lá
        self.assertEqual(self.pagina.ao_esconder(), pagina.OCIOSA)  # e é largada
        self.assertEqual(self.carregados, [pagina.OCIOSA])

    def test_estado_inicial_desconhecido_e_erro(self):
        with self.assertRaises(ValueError):
            pagina.PaginaDaJanela(lambda: None, lambda: None, estado_inicial="meio-carregada")

    def test_mostrar_carrega_a_pagina_do_app(self):
        self.assertEqual(self.pagina.ao_mostrar(), pagina.APP)
        self.assertEqual(self.pagina.estado, pagina.APP)
        self.assertEqual(self.carregados, [pagina.APP])

    def test_mostrar_de_novo_nao_recarrega_a_tela(self):
        self.pagina.ao_mostrar()
        self.assertEqual(self.pagina.ao_mostrar(), pagina.NADA)
        # um clique a mais na bandeja não pode jogar fora o que está na tela
        self.assertEqual(self.carregados, [pagina.APP])

    def test_esconder_volta_para_a_pagina_ociosa(self):
        self.pagina.ao_mostrar()
        self.assertEqual(self.pagina.ao_esconder(), pagina.OCIOSA)
        self.assertEqual(self.pagina.estado, pagina.OCIOSA)
        self.assertEqual(self.carregados, [pagina.APP, pagina.OCIOSA])

    def test_esconder_o_que_ja_esta_ocioso_nao_faz_nada(self):
        self.assertEqual(self.pagina.ao_esconder(), pagina.NADA)
        self.assertEqual(self.carregados, [])

    def test_ciclo_de_abrir_e_fechar_troca_uma_vez_por_ponta(self):
        for _ in range(3):
            self.pagina.ao_mostrar()
            self.pagina.ao_esconder()
        self.assertEqual(self.carregados, [pagina.APP, pagina.OCIOSA] * 3)
        self.assertEqual(self.pagina.resumo(), {"pagina": pagina.OCIOSA, "trocas": 6})

    def test_falha_ao_carregar_o_app_nao_mente_sobre_o_estado(self):
        self.pagina = pagina.PaginaDaJanela(_explode, lambda: None, log=self.logs.append)
        self.assertEqual(self.pagina.ao_mostrar(), pagina.FALHOU)
        self.assertEqual(self.pagina.estado, pagina.OCIOSA)
        self.assertEqual(self.pagina.resumo()["trocas"], 0)
        self.assertIn("não consegui trocar a página", self.logs[-1])

    def test_falha_ao_esconder_mantem_a_pagina_do_app(self):
        self.pagina = pagina.PaginaDaJanela(lambda: None, _explode, log=self.logs.append)
        self.pagina.ao_mostrar()
        self.assertEqual(self.pagina.ao_esconder(), pagina.FALHOU)
        self.assertEqual(self.pagina.estado, pagina.APP)
        self.assertEqual(self.pagina.resumo()["trocas"], 1)

    def test_depois_de_uma_falha_a_tentativa_seguinte_vale(self):
        tentativas = {"n": 0}

        def instavel() -> None:
            tentativas["n"] += 1
            if tentativas["n"] == 1:
                raise RuntimeError("primeira vez falhou")

        self.pagina = pagina.PaginaDaJanela(
            instavel, lambda: self.carregados.append(pagina.OCIOSA), log=self.logs.append
        )
        self.assertEqual(self.pagina.ao_mostrar(), pagina.FALHOU)
        self.assertEqual(self.pagina.ao_mostrar(), pagina.APP)
        self.assertEqual(self.pagina.ao_esconder(), pagina.OCIOSA)

    def test_a_pagina_ociosa_nao_tem_relogio(self):
        """É o ponto dela: sem timer, não há o que acumular enquanto escondida."""
        for relogio in ("setInterval", "setTimeout", "requestAnimationFrame"):
            self.assertNotIn(relogio, pagina.PAGINA_OCIOSA)

    def test_a_pagina_ociosa_perdoa_resposta_atrasada(self):
        """O único script dela é o que evita traceback de resposta atrasada."""
        self.assertIn("_returnValuesCallbacks", pagina.PAGINA_OCIOSA)
        self.assertIn("pywebviewready", pagina.PAGINA_OCIOSA)
