"""Resumo pelo Claude Code instalado na máquina (`claude -p`).

Escolhido como primeiro provedor porque não pede chave de API nem
configuração: quem usa o Fireball para tomar nota de reunião com o Claude já
tem o `claude` autenticado. O custo é do plano de quem roda, não uma
credencial que o Fireball precise guardar.

Detalhes que a invocação depende:

- `--output-format json`, e **não** o texto puro. Em modo texto o Claude Code
  sai com código 0 mesmo quando falha — "Not logged in · Please run /login" é
  impresso no stdout como se fosse resposta. O JSON traz `is_error`, que é o
  único jeito honesto de saber se deu certo.
- **Sem `--bare`**, por mais tentador que pareça. Ele pula hooks, LSP e
  plugins, o que soa perfeito para uma transformação de texto — mas pula
  também o que carrega a autenticação, e toda chamada volta como "Not logged
  in · Please run /login" mesmo com credencial válida no disco. Foi
  exatamente esse o sintoma que apareceu na janela.
- `--allowed-tools ""` porque não há nada para ele fazer além de ler o que
  mandamos: a transcrição vai no stdin, e sair lendo arquivo da máquina não é
  parte do trabalho.
- `cwd` na pasta da reunião, pelo mesmo motivo — o processo não tem por que
  enxergar o repositório de onde o daemon subiu.

O pedido em si (nome, tags e resumo numa resposta só, em JSON) é o comum a
todos os provedores e mora em `fireball/summarizers/prompt.py`; aqui só o
transporte.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from fireball.summarizers import SummarizerFailed, SummarizerUnavailable
from fireball.summarizers.prompt import build_prompt, parse_result

# Um resumo de reunião longa pode demorar; mais que isso é sinal de que algo
# travou, e o job ficaria pendurado sem ninguém para cobrar.
TIMEOUT_S = 600


class ClaudeCodeSummarizer:
    name = "claude_code"

    def summarize(self, transcript: str, meeting: dict) -> dict:
        binary = shutil.which("claude")
        if not binary:
            raise SummarizerUnavailable(
                "O comando 'claude' não foi encontrado no PATH. Instale o Claude Code "
                "(https://claude.com/claude-code) para gerar resumos com este provedor."
            )

        cmd = [
            binary,
            "-p",
            "--output-format",
            "json",
            "--allowed-tools",
            "",
            build_prompt(meeting),
        ]

        # a pasta da reunião existe e é o contexto natural deste trabalho
        cwd = Path(meeting.get("_dir") or ".")
        try:
            proc = subprocess.run(
                cmd,
                input=transcript,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                cwd=str(cwd) if cwd.is_dir() else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise SummarizerUnavailable(
                f"O Claude Code não respondeu em {TIMEOUT_S}s."
            ) from exc

        if proc.returncode != 0 and not proc.stdout.strip():
            detail = (proc.stderr or "").strip() or f"código {proc.returncode}"
            raise SummarizerUnavailable(f"O Claude Code falhou: {detail}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            head = (proc.stdout or proc.stderr or "").strip()[:300]
            raise SummarizerUnavailable(
                f"Resposta inesperada do Claude Code (não era JSON): {head!r}"
            ) from exc

        # dois JSON encaixados, e por motivos diferentes: o de fora é o
        # envelope do Claude Code (é dele que sai o `is_error`), o de dentro é
        # a resposta que pedimos no prompt.
        result = (payload.get("result") or "").strip()
        if payload.get("is_error"):
            # é aqui que cai o "Not logged in · Please run /login"
            raise SummarizerUnavailable(f"O Claude Code recusou: {result or 'erro sem mensagem'}")
        if not result:
            raise SummarizerFailed("O Claude Code devolveu uma resposta vazia.")
        return parse_result(result)
