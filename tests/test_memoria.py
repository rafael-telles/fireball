from __future__ import annotations

import unittest

from fireball.gui import memoria

STATUS = """Name:\tQtWebEngineProc
VmPeak:\t  204800 kB
VmSize:\t  204800 kB
VmRSS:\t  102400 kB
VmSwap:\t   10240 kB
"""


class DescendentesTests(unittest.TestCase):
    def test_acha_filhos_netos_e_bisnetos(self):
        pais = {1: 0, 10: 1, 11: 10, 12: 11, 13: 12, 99: 1}
        self.assertEqual(sorted(memoria.descendentes(10, pais)), [11, 12, 13])

    def test_folha_nao_tem_descendente(self):
        self.assertEqual(memoria.descendentes(13, {10: 1, 13: 10}), [])

    def test_pid_inexistente_nao_explode(self):
        self.assertEqual(memoria.descendentes(4242, {1: 0}), [])


class MemoriaMbTests(unittest.TestCase):
    def test_soma_rss_e_swap(self):
        self.assertAlmostEqual(memoria.memoria_mb(10, ler=lambda _p: STATUS), 110.0)

    def test_processo_que_ja_foi_conta_zero(self):
        def sumiu(_pid):
            raise FileNotFoundError("morreu")

        self.assertEqual(memoria.memoria_mb(10, ler=sumiu), 0.0)


class MemoriaDosRenderersTests(unittest.TestCase):
    def test_conta_so_o_webengine(self):
        """O daemon carrega os modelos de fala: isso não é conta da página."""
        pais = {1: 0, 100: 1, 101: 100, 102: 101, 103: 100}
        rss = {100: 400.0, 101: 120.0, 102: 30.0, 103: 2.0}
        lidos = []

        def ler(pid):
            lidos.append(pid)
            return f"VmRSS:\t {int(rss[pid] * 1024)} kB\n"

        mb = memoria.memoria_dos_renderers(
            100,
            pais=pais,
            ler=ler,
            e_webengine=lambda pid: pid in (101, 102),
        )
        self.assertAlmostEqual(mb, 150.0)
        self.assertNotIn(100, lidos)  # a raiz (o daemon) não entra na conta

    def test_webengine_com_leitura_proibida_nao_quebra(self):
        def ler(pid):
            raise PermissionError("environ ilegível")

        mb = memoria.memoria_dos_renderers(
            100, pais={100: 1, 101: 100}, ler=ler, e_webengine=lambda _p: True
        )
        self.assertEqual(mb, 0.0)


class DeveReciclarTests(unittest.TestCase):
    def test_abaixo_do_teto_nao_mexe(self):
        self.assertFalse(memoria.deve_reciclar(300))

    def test_no_teto_recicla(self):
        self.assertTrue(memoria.deve_reciclar(memoria.LIMITE_MB))
        self.assertTrue(memoria.deve_reciclar(memoria.LIMITE_MB + 1))

    def test_gravando_o_teto_e_mais_alto(self):
        """Recomeçar no meio da reunião apaga a tela que a pessoa está lendo."""
        self.assertFalse(memoria.deve_reciclar(memoria.LIMITE_MB + 100, gravando=True))
        self.assertTrue(memoria.deve_reciclar(memoria.LIMITE_GRAVANDO_MB, gravando=True))

    def test_teto_gravando_cabe_muito_antes_dos_16_gib_do_chromium(self):
        """O renderer aborta em 16 GiB de VmData; reciclar tem que ser antes."""
        self.assertLess(memoria.LIMITE_MB, 16 * 1024)
        self.assertLess(memoria.LIMITE_GRAVANDO_MB, 16 * 1024)


if __name__ == "__main__":
    unittest.main()
