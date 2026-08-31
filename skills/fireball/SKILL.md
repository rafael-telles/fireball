---
name: fireball
description: Acompanha uma reunião ao vivo com o Fireball — grava, transcreve e atua como escrivão/secretária, tomando notas em tempo real e sinalizando ações pendentes enquanto a reunião acontece.
---

# Fireball — escrivão de reuniões ao vivo

Você é o escrivão/secretária da reunião. O Fireball cuida da gravação e transcrição; você lê o
que está sendo dito em tempo real através do CLI `fireball` e decide o que anotar e o que propor
como ação.

## Iniciar uma reunião

1. `fireball start --real` → retorna `meeting_id`, grava microfone e
   áudio do sistema de verdade e já transcreve ao vivo (local, sem chave de API) enquanto grava.
   - **Não passe `--name`** a menos que o usuário tenha dito o nome da reunião. Sem ele a
     reunião fica sem nome e o provedor de resumo escreve um (junto com as tags) quando a
     gravação termina — e um nome dado à mão nunca é trocado depois.
   - `--backend whisper` (padrão, multi-idioma) ou `--backend parakeet` (Parakeet TDT
     fine-tunado em pt-BR — capturou mais conteúdo real em teste manual, prefira esse para
     reuniões em português se o modelo já estiver baixado, ver README).
   - Use `--fake` só para testar o resto do fluxo com uma transcrição simulada, sem gravar
     áudio nenhum.
   - Se nenhum backend estiver instalado, a gravação continua normalmente mas
     `transcript.ndjson` fica vazio (aviso em `transcribe_warnings.log`) — nesse caso, tome
     notas manualmente com base no que está ouvindo.
2. Avise o usuário que a reunião começou, o `meeting_id` e qual backend está transcrevendo.

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
2. `fireball finalize <meeting_id>` — roda a transcrição final (mais precisa, sobre o áudio
   inteiro de cada track, não em pedaços) com o mesmo backend usado ao vivo. Também aceita
   `--backend groq` (API paga, `whisper-large-v3-turbo`, requer `GROQ_API_KEY`) se o usuário
   preferir não depender do modelo local para a passada final.
3. O nome, as tags e o resumo da reunião saem sozinhos assim que a transcrição final fica
   pronta (`fireball summarize <meeting_id>` força de novo). Se a reunião ainda estiver sem
   nome depois disso, o provedor de resumo não está configurado — diga isso ao usuário em vez
   de inventar um nome com `fireball rename`.
4. Compare a transcrição (`fireball transcript show <meeting_id>`, já reescrita pelo finalize) com as
   notas ao vivo (arquivo `notes.md`, dentro da pasta da reunião). Corrija imprecisões
   diretamente no arquivo de notas e resuma para o usuário o que mudou. Isso é automático, não
   precisa ser pedido.
5. Revise a lista de ações pendentes (`fireball action list <meeting_id> --status pending`) com
   o usuário antes de encerrar.
6. Se a reunião pertencer a um vault Tolaria, siga as convenções do `AGENTS.md` daquele vault
   para arquivar a nota final (frontmatter, wikilinks, tipo).

## Onde ficam os arquivos

Cada reunião mora em `~/.fireball/meetings/<meeting_id>/`:

- `meeting.json` — metadados (inclusive `name`, `name_source` e `tags`)
- `mic.wav` / `system.wav` / `meeting.wav` — áudio real (só em reuniões `--real`)
- `transcript.ndjson` — transcrição em tempo real (fonte primária durante a reunião)
- a transcrição é um arquivo só (`transcript.ndjson`): o `finalize` reescreve por cima dela
- `notes.md` — notas ao vivo (a mesma fonte que a GUI reflete)
- `actions.json` — ações pendentes/aprovadas/rejeitadas/executadas
- `summary.md` — resumo gerado da transcrição
- `checkpoint.json` — até onde o stream já foi processado
