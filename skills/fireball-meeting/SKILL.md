---
name: fireball-meeting
description: Inicia uma reunião com o Fireball e escreve as notas ao vivo direto num vault Tolaria — cria a nota de Meeting, atualiza Notes/Action items em tempo real, e arquiva a Transcript ao final linkando as duas.
---

# Fireball + Tolaria — reunião com notas ao vivo no vault

Combina o CLI do Fireball (grava e transcreve) com as convenções de um vault Tolaria
(`AGENTS.md` na raiz do vault). Diferença chave em relação à skill genérica `fireball`: aqui a
**nota `Meeting` no vault é a fonte da verdade** das notas da reunião, não o `notes.md` do
Fireball.

Pressupõe que o vault já tem os tipos `Meeting` e `Transcript` definidos (`meeting.md` /
`transcript.md` na raiz) — não crie tipos novos, siga o schema que já existir lá.

CLI do Fireball (venv dedicado, não está no PATH):
`/home/rafaelt/Desktop/fireball/.venv/bin/fireball`

## 1. Iniciar

1. Se não estiver óbvio pela conversa, pergunte: nome da reunião, projeto relacionado
   (`related_to`) e participantes conhecidos (`attendees`). Não bloqueie o início por isso —
   prossiga com o que tiver e deixe em branco o que não souber.
2. `fireball start --name "<nome>" --real --backend parakeet` → guarde o `meeting_id`.
   - `parakeet` performou bem melhor que `whisper` em teste manual com fala em pt-BR; caia
     para `--backend whisper` se o modelo do parakeet não estiver baixado (ver README do
     Fireball).
3. Crie a nota `Meeting` no vault (raiz, kebab-case: `<slug-do-nome>-YYYY-MM-DD.md`):

   ```markdown
   ---
   type: Meeting
   date: "YYYY-MM-DD"
   status: In Progress
   related_to: "[[<projeto>]]"
   attendees:
     - <nome>
   transcript:
   ---
   # <Nome da reunião>

   ## Agenda

   ## Notes

   ## Action items
   ```

   Omita `related_to`/`attendees` (deixe vazio) se não souber — não invente valor.
4. Avise o usuário que a reunião começou, o `meeting_id` e o caminho da nota criada.

## 2. Acompanhar ao vivo

1. Rode em background (Bash, `run_in_background: true`):
   `/home/rafaelt/Desktop/fireball/.venv/bin/fireball transcript follow <meeting_id>`
2. Use o Monitor tool nesse processo. Cada linha de stdout é um segmento novo da reunião:
   `{"seq", "ts", "speaker", "text", "source"}`.
3. Para cada trecho relevante (decisão, contexto importante, pergunta, resumo de um ponto
   discutido): edite a seção `## Notes` da nota `Meeting` no vault, acrescentando uma linha
   objetiva em português. **Não** chame `fireball note` — isso escreveria no `notes.md` do
   Fireball, que não é o que este fluxo usa como fonte da verdade.
4. Se implicar uma ação concreta, acrescente uma linha em `## Action items` da nota:
   `- [ ] <título>`. **Não execute a ação de verdade** sem o usuário pedir.
5. Depois de processar um lote de segmentos, confirme o checkpoint (permite retomar do ponto
   certo se a sessão cair no meio da reunião):
   `fireball transcript ack <meeting_id> <seq>`
6. Quando o usuário mandar executar uma dessas ações, faça-a com a tool apropriada e atualize a
   linha correspondente em `## Action items` para `- [x] <título>`.

## 3. Encerrar

1. `fireball stop <meeting_id>`
2. `fireball finalize <meeting_id>` — mesmo backend do início por padrão; `--backend groq` se
   preferir a passada final via API (`GROQ_API_KEY` no `.env` do Fireball) em vez do modelo
   local.
3. Leia `~/.fireball/meetings/<meeting_id>/transcript.ndjson` (após o `finalize`, ela já é a versão
   feita sobre o áudio inteiro) e monte o corpo de uma nota
   `Transcript` nova no vault, uma linha por segmento, formato `[mm:ss] <speaker>: <texto>`
   (calcule mm:ss a partir de `start`; se vier `null`, omita o timestamp).

   **Crie essa nota sempre, mesmo se o conteúdo parecer irrelevante, ruído de fundo ou áudio
   ambiente.** A `Transcript` é o registro bruto e não-curado (é literalmente a definição do
   tipo) — a curadoria já aconteceu no `## Notes` da `Meeting`; não pule este passo por
   julgamento de relevância.

   ```markdown
   ---
   type: Transcript
   date: "YYYY-MM-DD"
   related_to: "[[<nota-da-reuniao>]]"
   ---
   # Transcript — <Nome da reunião>

   [00:00] Você: ...
   [00:05] Outros participantes: ...
   ```
4. Atualize a nota `Meeting`: `transcript: "[[<nota-transcript>]]"` e `status: Done`. Os dois
   campos são atualizados juntos — nunca marque `status: Done` sem o link de `transcript`
   apontar pra uma nota que de fato existe.
5. Compare a transcrição final com as notas já escritas em `## Notes` da nota `Meeting`.
   Corrija imprecisões diretamente ali e resuma pro usuário o que mudou — isso é automático,
   não precisa ser pedido.
6. Revise com o usuário a lista de ações ainda pendentes em `## Action items` antes de
   encerrar.

## Convenções do vault (ver `AGENTS.md` na raiz do vault)

- Primeiro H1 = título da nota. Nomes de arquivo em kebab-case, na raiz do vault.
- `related_to` como wikilink entre aspas para valor único (`"[[nome]]"`); lista de wikilinks
  para múltiplos valores.
- Propriedades que você não souber preencher (ex.: `attendees` sem participantes conhecidos)
  ficam vazias — não invente valor.
