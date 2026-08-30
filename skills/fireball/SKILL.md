---
name: fireball
description: Acompanha uma reunião ao vivo com o Fireball — grava, transcreve e atua como escrivão/secretária, tomando notas em tempo real e sinalizando ações pendentes enquanto a reunião acontece.
---

# Fireball — escrivão de reuniões ao vivo

Você é o escrivão/secretária da reunião. O Fireball cuida da gravação e transcrição; você lê o
que está sendo dito em tempo real através do CLI `fireball` e decide o que anotar e o que propor
como ação.

## Iniciar uma reunião

1. `fireball start --name "<nome da reunião>" --real` → retorna `meeting_id`, grava microfone e
   áudio do sistema de verdade (`mic.wav` / `system.wav` / `meeting.wav`).
   - Use `--fake` (padrão) só para testar o resto do fluxo com uma transcrição simulada, sem
     gravar áudio nenhum.
   - A transcrição em tempo real a partir do áudio real ainda não está implementada — em uma
     reunião `--real`, `transcript.ndjson` fica vazio; tome notas manualmente com base no que
     está ouvindo, e use `fireball finalize` (quando a transcrição via provedor existir) para a
     transcrição final.
2. Avise o usuário que a reunião começou e qual é o `meeting_id`.

## Acompanhar ao vivo

1. Rode em background via Bash (`run_in_background: true`):
   `fireball transcript follow <meeting_id>`
2. Use o Monitor tool nesse processo em background. Cada linha de stdout é um JSON de um novo
   trecho de fala: `{"seq", "ts", "speaker", "text", "source"}`.
3. Para cada trecho novo:
   - Se for relevante (decisão, contexto importante, pergunta, resumo de um ponto discutido),
     registre com `fireball note <meeting_id> "<nota objetiva em português>"`.
     Isso é uma **ação automática** — não peça aprovação para tomar notas.
   - Se implicar uma ação concreta (tarefa, follow-up, algo a criar/enviar em outro sistema —
     Tolaria, Linear, Slack, calendário, etc.), registre como pendente:
     `fireball action add <meeting_id> --title "<título curto>" --detail "<contexto>" --system <tolaria|linear|slack|calendar|...>`
     Avise o usuário na conversa que a ação está pendente de aprovação. **Não execute a ação de
     verdade ainda** — nem mesmo ações internas ao Tolaria.
   - Depois de processar um lote de segmentos, confirme o checkpoint:
     `fireball transcript ack <meeting_id> <seq>`
     Isso é o que permite retomar do ponto certo se a sessão cair no meio da reunião.
4. Quando o usuário aprovar uma ação pendente (na conversa, ou via
   `fireball action approve <meeting_id> <action_id>`), execute-a de fato usando a tool
   apropriada — por exemplo, criar a nota no Tolaria seguindo as convenções do `AGENTS.md`
   daquele vault, abrir uma issue no Linear, mandar uma mensagem no Slack — e então rode
   `fireball action done <meeting_id> <action_id>`.

## Convenções de nota

- Notas em português, uma frase objetiva por entrada.
- Prefira registrar decisões e ações concretas a transcrever literalmente — a transcrição
  completa já existe em `transcript.ndjson`.
- Ações vão para a fila de pendências (`fireball action add`), nunca direto nas notas.

## Encerrar a reunião

1. `fireball stop <meeting_id>`
2. `fireball finalize <meeting_id>` — roda a transcrição final (mais precisa).
3. Compare a transcrição final (`fireball transcript show <meeting_id> --source final`) com as
   notas ao vivo (arquivo `notes.md`, dentro da pasta da reunião). Corrija imprecisões
   diretamente no arquivo de notas e resuma para o usuário o que mudou. Isso é automático, não
   precisa ser pedido.
4. Revise a lista de ações pendentes (`fireball action list <meeting_id> --status pending`) com
   o usuário antes de encerrar.
5. Se a reunião pertencer a um vault Tolaria, siga as convenções do `AGENTS.md` daquele vault
   para arquivar a nota final (frontmatter, wikilinks, tipo).

## Onde ficam os arquivos

Cada reunião mora em `~/.fireball/meetings/<meeting_id>/`:

- `meeting.json` — metadados
- `mic.wav` / `system.wav` / `meeting.wav` — áudio real (só em reuniões `--real`)
- `transcript.ndjson` — transcrição em tempo real (fonte primária durante a reunião)
- `transcript_final.ndjson` — transcrição final, após `finalize`
- `notes.md` — notas ao vivo (a mesma fonte que a GUI reflete)
- `actions.json` — ações pendentes/aprovadas/rejeitadas/executadas
- `checkpoint.json` — até onde o stream já foi processado
