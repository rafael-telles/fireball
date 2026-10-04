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
`/home/rafael/Desktop/fireball/.venv/bin/fireball`

## 1. Iniciar

1. Se não estiver óbvio pela conversa, pergunte: nome da reunião, projeto relacionado
   (`related_to`) e participantes conhecidos (`attendees`). Não bloqueie o início por isso —
   prossiga com o que tiver e deixe em branco o que não souber.
2. `fireball start --name "<nome>" --real --backend parakeet` → guarde o `meeting_id`.
   - `parakeet` performou bem melhor que `whisper` em teste manual com fala em pt-BR; caia
     para `--backend whisper` se o modelo do parakeet não estiver baixado (ver README do
     Fireball).
   - Acrescente `--diarize` somente se o usuário quiser separar falantes nesta
     reunião. A transcrição ao vivo usa `Sala N` / `Remoto N`; o `finalize`
     processa o áudio novamente e refina esses rótulos.
3. Crie a nota `Meeting` no vault em
   `fireball/<YYYY-MM-DD>/<slug-do-nome>/resumo.md` — a transcrição vai depois para
   `transcricao.md`, na mesma pasta:

   ```markdown
   ---
   type: Meeting
   date: "YYYY-MM-DD"
   related_to: "[[<projeto>]]"
   attendees:
     - <nome>
   datetime: "YYYY-MM-DD HH:MM"
   fireball_id: "<meeting_id>"
   _organized: true
   ---
   # <Nome da reunião> (YYYY-MM-DD HH:MM)

   ## Agenda

   _(Se a reunião nasceu de um evento — `fireball start --event` ou clique na
   home — a pauta/descrição do calendário pode ir aqui.)_

   ## Notes

   ## Action items
   ```

   Omita `related_to`/`attendees` (deixe vazio) se não souber — não invente valor.
   `_organized: true` desde a criação, senão a nota nasce na Inbox do Tolaria. `datetime` é o
   início em horário local, com data e hora no mesmo campo — `date` fica só com o dia, para a
   visão por data. `tags` e `duration` só entram no fim, quando houver o que preencher.

   Se a reunião começou sem nome (ex.: iniciada pela GUI) e ganhar um nome melhor no fim,
   renomeie a pasta, os dois H1 e o link entre as notas — slug e título seguem o nome final.
4. Avise o usuário que a reunião começou, o `meeting_id` e o caminho da nota criada.

## 2. Acompanhar ao vivo

1. Rode em background (Bash, `run_in_background: true`):
   `/home/rafael/Desktop/fireball/.venv/bin/fireball transcript follow <meeting_id>`
2. Use o Monitor tool nesse processo. Cada linha de stdout é um segmento novo da reunião:
   `{"seq", "ts", "speaker", "text", "source"}`.
3. Para cada trecho relevante (decisão, contexto importante, pergunta, resumo de um ponto
   discutido): acrescente uma linha objetiva em português sob `### Notas ao vivo`, dentro de
   `## Notes` do `resumo.md` (crie a subseção na primeira nota). **Não** chame `fireball note`
   — isso escreveria no `notes.md` do Fireball, que não é a fonte da verdade deste fluxo.
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
   local. Com `transcribe_final` desligado na configuração o comando **recusa**: siga com a
   transcrição do tempo real, que é o que o usuário pediu ao desligar.
   Com diarização ligada, os rótulos finais são `Sala N` (microfone) e
   `Remoto N` (áudio do sistema), exceto quando um perfil de voz já cadastrado
   for reconhecido com confiança. Não associe convidados da agenda a rótulos
   anônimos sem confirmação explícita.
3. Leia `~/.fireball/meetings/<meeting_id>/transcript.ndjson` (após o `finalize`, ela já é a
   versão feita sobre o áudio inteiro) e monte o corpo de uma nota
   `Transcript` nova em `fireball/<YYYY-MM-DD>/<slug-do-nome>/transcricao.md` (mesma pasta do
   `resumo.md`), uma linha por segmento, formato `[mm:ss] <speaker>: <texto>`
   (calcule mm:ss a partir de `start`; se vier `null`, omita o timestamp).

   **Crie essa nota sempre, mesmo se o conteúdo parecer irrelevante, ruído de fundo ou áudio
   ambiente.** A `Transcript` é o registro bruto e não-curado (é literalmente a definição do
   tipo) — a curadoria já aconteceu no `## Notes` da `Meeting`; não pule este passo por
   julgamento de relevância.

   ```markdown
   ---
   type: Transcript
   date: "YYYY-MM-DD"
   datetime: "YYYY-MM-DD HH:MM"
   related_to: "[[<Nome da reunião> (YYYY-MM-DD HH:MM)]]"
   fireball_id: "<meeting_id>"
   segments: <quantidade de linhas>
   _organized: true
   ---
   # Transcript — <Nome da reunião> (YYYY-MM-DD HH:MM)

   [00:00] Você: ...
   [00:05] Outros participantes: ...
   ```
4. Complete o frontmatter do `resumo.md`:
   - a `Transcript` em `related_to`, pelo título:
     `"[[Transcript — <Nome da reunião> (YYYY-MM-DD HH:MM)]]"`. Vira uma lista YAML quando já
     houver um projeto ali, e só entra depois que a nota de transcrição existir de fato.
   - `duration` no formato `"23min"` / `"1h27min"`.
   - `tags`: 3 a 5, em minúsculas, reaproveitando o vocabulário das outras reuniões do vault.
5. Compare a transcrição final com as notas já escritas em `## Notes` da nota `Meeting`.
   Corrija imprecisões diretamente ali e resuma pro usuário o que mudou — isso é automático,
   não precisa ser pedido.
6. Feche o `## Notes` com a curadoria, em subseções acima de `### Notas ao vivo`: um parágrafo
   curto dizendo o que a reunião resolveu, depois `### Decisões` e `### Em aberto`. Omita a
   seção que ficaria vazia — não invente item para preencher.
7. Revise com o usuário a lista de ações ainda pendentes em `## Action items` antes de
   encerrar.

## Convenções do vault (ver `AGENTS.md` na raiz do vault)

- Primeiro H1 = título da nota.
- As notas de reunião moram em `fireball/<YYYY-MM-DD>/<slug-do-nome>/`, duas por pasta:
  `resumo.md` (a `Meeting`) e `transcricao.md` (a `Transcript`). A pasta do dia e o slug em
  kebab-case carregam a identificação — os arquivos em si têm sempre esses dois nomes.
- O H1 termina com data e hora entre parênteses: `# <Nome da reunião> (YYYY-MM-DD HH:MM)` e
  `# Transcript — <Nome da reunião> (YYYY-MM-DD HH:MM)`.
- Como os nomes de arquivo se repetem entre as pastas, **os wikilinks são pelo título (H1)**,
  não pelo nome do arquivo. Por isso o H1 precisa ser único no vault — são a data e a hora no
  fim que garantem isso quando uma reunião recorrente se repete, inclusive duas vezes no
  mesmo dia.
- `related_to` como wikilink entre aspas para valor único (`"[[título]]"`); lista de wikilinks
  para múltiplos valores.
- Propriedades que você não souber preencher (ex.: `attendees` sem participantes conhecidos)
  ficam vazias — não invente valor.
- Uma pessoa entra uma vez só em `attendees`. A mesma pessoa costuma chegar por caminhos
  diferentes (convite da agenda, perfil de voz, nome sem e-mail) e às vezes com endereços
  distintos — prefira o nome completo e não repita quem já está na lista sob outro e-mail.
  Quem não tem nome em lugar nenhum fica pelo e-mail mesmo; não invente um nome a partir dele.
