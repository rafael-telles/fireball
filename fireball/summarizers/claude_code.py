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
- `--bare` pula hooks, LSP e plugins: aqui é uma transformação de texto, não
  uma sessão de trabalho, e o ambiente de outra pessoa não deve influenciar o
  resumo.
- `--allowed-tools ""` porque não há nada para ele fazer além de ler o que
  mandamos: a transcrição vai no stdin, e sair lendo arquivo da máquina não é
  parte do trabalho.
- `cwd` na pasta da reunião, pelo mesmo motivo — o processo não tem por que
  enxergar o repositório de onde o daemon subiu.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from fireball.summarizers import SummarizerUnavailable

# Um resumo de reunião longa pode demorar; mais que isso é sinal de que algo
# travou, e o job ficaria pendurado sem ninguém para cobrar.
TIMEOUT_S = 600

PROMPT = """\
Você está resumindo a transcrição de uma reunião gravada pelo Fireball.

A transcrição chega pelo stdin, uma fala por linha, no formato `Falante: texto`.
Ela vem de transcrição automática: espere erros de palavra, pontuação estranha
e frases cortadas. Interprete pelo contexto e não cite trechos que não fazem
sentido.

Dois falantes são fixos e significam o seguinte:
- "Você" é quem gravou (o microfone da máquina).
- "Outros participantes" é todo o resto da sala junto — o áudio do sistema não
  distingue quem fala do outro lado, então não invente nomes nem atribua uma
  fala a alguém específico.

Escreva em português do Brasil, em markdown, nesta estrutura:

# <título curto que diga o assunto da reunião>

<um parágrafo, no máximo três frases, dizendo o que a reunião resolveu>

## Decisões
- <o que ficou decidido; omita a seção inteira se nada foi decidido>

## Em aberto
- <o que ficou sem resposta; omita a seção inteira se não houver>

Regras: não invente nada que não esteja na transcrição; se ela for curta ou
não tiver conteúdo real, diga isso em uma frase em vez de preencher as seções.
Responda só com o markdown do resumo, sem comentário antes ou depois.\
"""


class ClaudeCodeSummarizer:
    name = "claude_code"

    def summarize(self, transcript: str, meeting: dict) -> str:
        binary = shutil.which("claude")
        if not binary:
            raise SummarizerUnavailable(
                "O comando 'claude' não foi encontrado no PATH. Instale o Claude Code "
                "(https://claude.com/claude-code) para gerar resumos com este provedor."
            )

        name = meeting.get("name") or meeting.get("id") or "reunião"
        cmd = [
            binary,
            "-p",
            "--bare",
            "--output-format",
            "json",
            "--allowed-tools",
            "",
            f"{PROMPT}\n\nO nome dado a esta reunião foi: {name}",
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

        result = (payload.get("result") or "").strip()
        if payload.get("is_error"):
            # é aqui que cai o "Not logged in · Please run /login"
            raise SummarizerUnavailable(f"O Claude Code recusou: {result or 'erro sem mensagem'}")
        if not result:
            raise SummarizerUnavailable("O Claude Code devolveu um resumo vazio.")
        return result
