"""Qual página a janela carrega — e por que ela não fica carregada à toa.

O daemon do Fireball fica de pé por semanas e a janela nasce escondida: o que
aparece na sessão é a bandeja. Se a página do app ficasse carregada durante
todo esse tempo, o renderer do QtWebEngine acumularia tudo que foi desenhado
sem nunca devolver. Medido num daemon de 2,6 dias: 3,6 GB de heap do Chromium
mais 1,1 GB de heap do JS, com mais de 5 GB disso parados no swap — o Fireball
virava o maior consumidor de memória da máquina.

Daí a regra: enquanto ninguém está olhando, a janela carrega uma página vazia
(sem relógio, nada para acumular), e a página do app só entra quando a janela
aparece — saindo de novo quando ela some. Reiniciar a cada abertura tem um
preço conhecido: a tela volta para o Início (o estado da tela não sobrevive à
troca).

Quem de fato carrega o HTML é a casca gráfica, injetado aqui como callable: a
decisão (carregar, não mexer, o que registrar) é pura, sem Qt e sem pywebview,
e por isso dá para testar sem abrir janela nenhuma.
"""

from __future__ import annotations

from typing import Callable, Optional

# os três estados possíveis da janela
APP = "app"
OCIOSA = "ociosa"

# o que `ao_mostrar`/`ao_esconder` devolvem
NADA = "nada"
FALHOU = "falhou"

# A página ociosa: fundo igual ao da janela, o nome do app e **nenhum
# relógio**. Não é enfeite — é o ponto. Um timer aqui ressuscitaria o
# problema que ela existe para resolver.
#
# O único script é o remendo de resposta atrasada: o pywebview entrega o
# resultado de uma chamada do JS com um `evaluate_js` na página que estiver
# carregada (webview/util.py), e o Chromium pode rodar esse JS só depois da
# troca de página — quando o callback daquela chamada já não existe e o log do
# daemon ganha um `JavascriptException`. O proxy aceita e joga fora esse resto
# (o mesmo remendo está em `web/app.js`, do outro lado da troca).
PAGINA_OCIOSA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Fireball</title>
<style>
  html, body { margin: 0; height: 100%; background: #0c0b0d; }
  body { display: grid; place-items: center; color: #55535c;
         font: 13px/1.4 system-ui, -apple-system, sans-serif; }
</style>
</head>
<body>Fireball está na bandeja</body>
<script>
  window.addEventListener("pywebviewready", () => {
    const registro = window.pywebview && window.pywebview._returnValuesCallbacks;
    if (!registro) return;
    // o que está registrado passa; o que não existe vira no-op
    window.pywebview._returnValuesCallbacks = new Proxy(registro, {
      get: (alvo, chave) => alvo[chave] ?? new Proxy({}, { get: () => () => {} }),
    });
  });
</script>
</html>
"""


class PaginaDaJanela:
    """Cuida de qual página está carregada: a do app ou a vazia.

    Nasce `ociosa` por padrão: quem cria a janela escondida não precisa do app
    carregado antes de alguém pedir para ver. Quem criar a janela já com a
    página do app (é o caso do pywebview, que carrega a URL na criação) passa
    `estado_inicial=APP` — senão o primeiro `ao_esconder()` vira no-op e a
    página do app fica carregada achando que foi largada.
    """

    def __init__(
        self,
        carregar_app: Callable[[], None],
        carregar_ociosa: Callable[[], None],
        estado_inicial: str = OCIOSA,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        if estado_inicial not in (APP, OCIOSA):
            raise ValueError(f"estado inicial desconhecido: {estado_inicial!r}")
        self._carregar_app = carregar_app
        self._carregar_ociosa = carregar_ociosa
        self._log = log if log is not None else (lambda _mensagem: None)
        self._estado = estado_inicial
        self._trocas = 0

    @property
    def estado(self) -> str:
        """`app` (página do app na janela) ou `ociosa` (página vazia)."""
        return self._estado

    def resumo(self) -> dict:
        """Para log e diagnóstico: onde a janela está e quantas trocas houve."""
        return {"pagina": self._estado, "trocas": self._trocas}

    def ao_mostrar(self) -> str:
        """A janela apareceu: carrega a página do app, se ela não estiver lá.

        Devolve o que fez — `app` quando carregou agora, `nada` quando a página
        do app já estava carregada (não recarregar é metade do ponto: senão
        cada clique na bandeja jogaria fora a tela que a pessoa estava lendo) e
        `falhou` quando o carregamento deu erro.
        """
        if self._estado == APP:
            return NADA
        return self._trocar(APP, self._carregar_app, "app carregado (janela em uso)")

    def ao_esconder(self) -> str:
        """A janela sumiu: larga a página do app e volta para a vazia.

        É aqui que o renderer devolve o que acumulou — o motivo de tudo isso.
        """
        if self._estado == OCIOSA:
            return NADA
        return self._trocar(OCIOSA, self._carregar_ociosa, "página ociosa (memória devolvida)")

    def _trocar(self, estado: str, carregar: Callable[[], None], mensagem: str) -> str:
        try:
            carregar()
        except Exception as exc:  # noqa: BLE001 — falhar aqui não derruba a bandeja
            # o estado fica o de antes: se a troca não aconteceu, dizer que
            # aconteceu esconderia a janela travada na página errada
            self._log(f"não consegui trocar a página: {exc}")
            return FALHOU
        self._estado = estado
        self._trocas += 1
        self._log(mensagem)
        return estado
