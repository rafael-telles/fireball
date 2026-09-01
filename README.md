# Fireball

Esboço do pivô do Fireball para skill + tools do Claude Code. Ver a nota de projeto no vault
Tolaria (`fireball.md`) para o desenho completo; este repositório é a primeira fatia executável:
o CLI e a skill.

## Status

Sketch inicial. O que funciona:

- CLI completo (`fireball`) com dois motores:
  - `--fake` (padrão): transcrição **simulada**, para exercitar todo o fluxo (gravação →
    transcrição ao vivo → notas → transcrição final) sem depender de
    microfone ou chaves de API.
  - `--real`: captura **de verdade** o microfone e o áudio do sistema (o que os outros
    participantes da reunião estão falando, via monitor da saída padrão), gravando
    `mic.wav`, `system.wav` e uma mixagem `meeting.wav` por reunião — **e transcreve em
    tempo real de verdade**, local, sem chave de API (ver Backends de transcrição abaixo).
- Transcrição final (`fireball finalize`) roda o backend escolhido sobre cada track,
  cortada pelo VAD em blocos de fala (mais preciso que o tempo real), e mescla mic/system
  por ordem de início — local (whisper/parakeet) ou via API da Groq (`--backend groq`, só
  pra transcrição final).
- Skill (`skills/fireball/SKILL.md`) com as instruções de como o Claude deve agir como escrivão.
- **Daemon** (`fireball.daemon`) — o processo dono do estado, que supervisiona as gravações,
  garante uma reunião por vez e **mostra o ícone na bandeja**: enquanto o Fireball está ligado,
  ele aparece lá. Ver seção própria abaixo.
- **Resumo, nome e tags** (`fireball summarize`) — um provedor plugável lê a transcrição e
  escreve as três coisas de uma vez. Reunião nasce **sem nome**: quem nomeia é a IA, quando a
  gravação termina. Dois provedores hoje: o Claude Code da máquina e qualquer API compatível
  com OpenAI (URL, chave e modelo na configuração). O **prompt é configurável** — uma lista
  administrada na tela de configuração, com um padrão, escolhível na hora de gerar. Ver seção
  própria abaixo.
- GUI + bandeja — barra lateral com o histórico e a reunião em andamento (pausar/retomar,
  parar), e a reunião aberta
  em três abas (Resumo · Transcrição · Notas): transcrição ao vivo em formato de chat, editor
  do `notes.md`, ficha com nome/tags/finalizar/abrir a pasta. `fireball-gui` abre a janela do
  daemon. Ver seção própria abaixo.

O que **não** está implementado ainda:

- Diarização de verdade para "Outros participantes" (hoje é só *um* falante genérico —
  o monitor do sistema não distingue quem está falando do outro lado).
- Anotar pela bandeja, e escolher outro device de captura quando o microfone falha (hoje
  o aviso aparece, mas a troca é por flag na CLI).

## Daemon

O Fireball tem **um processo dono do estado**, e ele é o mesmo processo que mostra o ícone na
bandeja. **Enquanto o Fireball está ligado, ele aparece na bandeja** — não existe daemon rodando
sem ícone nem ícone sem daemon. O ícone é a resposta visual à pergunta "isso está ligado?".

```
                          ┌───────────── daemon ─────────────┐
fireball (CLI) ─ socket ─→│  estado (reunião ativa, lock)     │─→ engine (gravação/transcrição)
                          │  bandeja + janela (Qt)            │─→ finalize (transcrição final)
fireball-gui  ─ socket ─→ │  meeting.json, notes.md           │
   ("abre a janela")      └──────────────────────────────────┘
```

A CLI não lê nem escreve `meeting.json`, não sobe processo de gravação e não guarda estado
próprio: fala com o daemon por socket Unix, e ele responde de memória. A janela é desenhada pelo
próprio daemon, então chama o núcleo direto, em processo — continua havendo **uma autoridade
só**, sob o mesmo lock (ver `fireball/gui/api.py`).

O daemon **sobe sozinho** no primeiro comando que precisar dele; não é preciso iniciá-lo à mão.
Num ambiente gráfico, isso significa que rodar `fireball start` no terminal já acende o ícone.

```bash
fireball daemon status     # está de pé? tem bandeja? o que está rodando?
fireball daemon start      # sobe em background (idempotente) — e acende a bandeja
fireball daemon stop       # desliga — apaga a bandeja e para a reunião ativa, fechando os arquivos
fireball daemon run        # foreground, com log no terminal (pra depurar)
fireball daemon run --no-tray   # sem casca gráfica, mesmo tendo display
```

### Sem sessão gráfica

O daemon continua subindo normalmente onde não há onde desenhar (servidor, ssh, extra `[gui]`
não instalado): ele registra o motivo no log, roda sem casca e a CLI funciona igual. `fireball
daemon status` mostra isso no campo `shell`, e `fireball-gui` avisa que não há janela em vez de
falhar de forma obscura.

```
$ fireball daemon status
{"running": true, ..., "shell": false, "active": null}
```

### Por que um daemon, e o que ele garante

**Uma reunião por vez, de verdade.** A reunião ativa é um campo em memória protegido por lock,
num processo único. Antes isso era *derivado* do filesystem (varrer todo `meeting.json`
procurando `status: "recording"`), o que é check-then-act: dois `fireball start` simultâneos
passavam os dois pela verificação e disputavam o microfone. Hoje, cinco `start` ao mesmo tempo
resultam em uma reunião e quatro recusas.

**Ninguém fica preso em `recording`.** O daemon supervisiona cada processo filho numa thread
(`proc.wait()`) e escreve o status final quando ele morre — `stopped` se saiu limpo, `crashed`
com o `exit_code` se não. Sem isso, um engine que terminava sozinho (roteiro `--fake`) ou que
quebrava (microfone ocupado) deixava `recording` gravado para sempre, e o app inteiro travava:
nenhuma reunião nova era aceita até editar o JSON na mão.

**`stop` não mente.** O engine ainda empacota os `.wav` e drena a última transcrição *depois*
do SIGTERM. O status vai para `stopping` e só vira `stopped` quando o processo realmente sai —
então `fireball finalize` logo em seguida sempre encontra o áudio.

**Nada fica órfão, e nada é morto por engano.** Se o daemon for morto sem cerimônia, o engine
sobrevive; o próximo daemon a subir **adota** o processo órfão (conferindo o `/proc/<pid>/cmdline`)
e volta a supervisioná-lo, ou marca a reunião como `crashed` se ele já se foi. Essa mesma
conferência evita o risco antigo de mandar SIGTERM para um PID reciclado pelo sistema.

**Um daemon só.** `flock` exclusivo em `$FIREBALL_HOME/daemon.lock` — duas subidas simultâneas
resolvem aí, sem janela de corrida; quem perde sai quieto e usa o socket de quem ganhou.

### Estados de uma reunião

`starting` → `recording` → `stopping` → `stopped` → `finalizing` → `finalized`

`recording` ↔ `paused` enquanto a reunião estiver de pé (ver *Pausar* abaixo).

Fora do caminho feliz: `crashed` (o engine morreu sozinho), `failed` (o engine nem subiu),
`finalize_failed`. `starting`, `recording`, `paused` e `stopping` contam como "viva" — é o que
`transcript follow` usa pra saber quando parar, e o que impede começar uma segunda reunião.
`paused` está nessa lista de propósito: o engine continua de pé e o microfone continua tomado,
ela só parou de capturar.

### Pausar e retomar

```bash
fireball pause     # a reunião que estiver gravando
fireball resume
```

Pausar **derruba os `parecord`** e retomar sobe outros dando append no mesmo PCM. A primeira
implementação usava `SIGSTOP`/`SIGCONT`, que é mais simples e está errada: com o cliente
parado o PulseAudio *enfileira* em vez de descartar, e no `SIGCONT` o `parecord` despeja tudo
— medido, 3,7s de pausa devolveram ~3,7s de áudio na retomada. A pausa só atrasava o áudio em
vez de descartá-lo, que é o oposto do que se espera ao pausar.

Como o PCM continua sendo um arquivo contínuo, "segundo tal da gravação" segue valendo do
começo ao fim: nada precisa compensar a pausa para a transcrição continuar alinhada ao áudio.
O que se perde é o buffer em voo de cada `parecord` encerrado — por isso a captura roda com
`--latency-msec=200` em vez do padrão (~2s), o que derruba a perda por pausa para o
imperceptível e, de quebra, faz a fala chegar à transcrição ao vivo quase na hora.

### Excluir

```bash
fireball delete <meeting_id>          # pergunta antes
fireball delete <meeting_id> --yes
```

Apaga a pasta inteira — áudio, transcrições, notas. Não há lixeira, e o daemon recusa
enquanto houver processo mexendo na reunião (gravando, finalizando ou resumindo): apagar
debaixo de um job que está escrevendo ali deixaria arquivo órfão e o job falharia sem
explicação. Na janela, o botão fica no pé da ficha e pede um segundo clique para confirmar.

### Protocolo

Uma linha JSON por mensagem, sobre um socket Unix em `$FIREBALL_HOME/daemon.sock` (ou, se esse
caminho estourar o limite de 108 bytes do `AF_UNIX`, um nome derivado por hash em
`$XDG_RUNTIME_DIR` — `fireball daemon status` mostra qual está em uso). Sem dependência externa
e sem porta TCP: é local, de um usuário só, e a permissão `0600` do socket já é a autenticação.

```
pedido    {"op": "start", "args": {"name": "Reunião", "fake": false}}
resposta  {"ok": true, "result": {...}}
          {"ok": false, "error": {"kind": "MeetingAlreadyActive", "message": "..."}}
```

A **única** coisa que não passa pelo daemon é a *leitura* da transcrição
(`transcript show/follow`), que lê `transcript.ndjson` direto do disco: é um stream contínuo com
um escritor só (o engine), e passá-lo pelo socket não daria nenhuma garantia a mais. Regra geral:
**escrita passa pelo daemon; o stream de leitura vai direto no arquivo.**

## Backends de transcrição (`--backend`)

A transcrição em tempo real e a final usam a mesma interface plugável (`fireball/backends/`),
trocável via `--backend`:

- **`whisper`** (padrão) — [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (CTranslate2), multi-idioma, modelo `base` por padrão.
- **`parakeet`** — [onnx-asr](https://github.com/istupakov/onnx-asr) rodando o
  [Parakeet TDT 0.6B v3 fine-tunado em pt-BR](https://huggingface.co/alefiury/parakeet-tdt-0.6b-v3-ptBR-TAGARELA-onnx)
  (dataset TAGARELA), via ONNX Runtime puro — **sem PyTorch/NeMo toolkit**. Em teste manual,
  capturou bem mais conteúdo real do que o whisper `base` no mesmo áudio.

Os dois são **locais** (sem chave de API) e independentes do segmentador de fala: em vez de
cortar em janelas de tempo fixo (que quebram frase no meio — testei e piora muito a
qualidade), o loop ao vivo (`fireball/realtime.py`) usa um VAD (`fireball/vad.py`, Silero via
faster-whisper) pra agrupar áudio em "falas" reais, entre pausas de silêncio, antes de mandar
pra qualquer backend. **Por isso o extra `whisper` é necessário mesmo escolhendo
`--backend parakeet`** — ele fornece o VAD, não só o motor de transcrição; desacoplar isso é
um TODO.

Instalar:

```bash
pip install -e '.[whisper]'     # backend whisper (e o VAD usado por qualquer backend)
pip install -e '.[parakeet]'    # inclui onnx-asr + whisper (pro VAD)
```

O checkpoint pt-BR do Parakeet não está na lista curada do onnx-asr, então precisa ser
baixado manualmente uma vez (± 2.4GB, fp32) antes do primeiro uso:

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download(
    repo_id='alefiury/parakeet-tdt-0.6b-v3-ptBR-TAGARELA-onnx',
    local_dir='.models/parakeet-ptbr')"
```

Por padrão o backend procura em `<repo>/.models/parakeet-ptbr`; aponte para outro lugar com
`FIREBALL_PARAKEET_MODEL_DIR=/caminho/pro/modelo`.

Usar:

```bash
fireball start --name "Reunião" --real --backend whisper    # padrão
fireball start --name "Reunião" --real --backend parakeet
fireball start --name "Reunião" --real --no-transcribe      # só grava, sem transcrever ao vivo
```

### A transcrição final também corta pelo VAD

`finalize` não manda o `.wav` inteiro para o backend: passa o VAD antes e transcreve
**bloco de fala** por bloco de fala, somando o início do bloco aos tempos que voltam.

Mandar a track inteira dava timestamp errado. O Whisper estica um segmento por cima da
pausa que veio antes dele — numa reunião de 43s, uma fala que acontece aos 24s voltou como
`0 → 27.64`, e outra aos 37s voltou como `30 → 59.98`, passando da duração do arquivo. O
texto estava certo; a posição, não. E como o merge das duas tracks ordena por `start`, a
fala do outro participante subia para o topo da conversa. A transcrição ao vivo não tinha
esse problema justamente porque ela já cortava pelo VAD antes de transcrever.

O corte é **por bloco, não por fala**: falas separadas por menos de 1,5s continuam juntas,
porque o contexto ao redor melhora a transcrição e porque assim o número de chamadas de API
acompanha as pausas, não as frases. Só o silêncio longo é descartado — o que, de quebra,
tira a alucinação em silêncio na origem, já que ele nunca chega ao modelo.

Efeito colateral bem-vindo: o `parakeet`, que não devolve timestamp nenhum, passa a ter os
tempos do bloco em vez de todo segmento ancorado no início da reunião.

### `groq` — transcrição final via API (só para `finalize`)

Terceiro backend, **só para lote** (`BatchBackend`, não implementa tempo real — é uma API
paga por requisição, chamar por "fala" no loop ao vivo geraria uma requisição a cada poucos
segundos). Usa `whisper-large-v3-turbo` hospedado pela Groq.

```bash
pip install -e '.[groq]'
export GROQ_API_KEY=...   # https://console.groq.com/keys
fireball finalize <meeting_id> --backend groq
```

A chave também sai da configuração (`groq_api_key`, com campo próprio na tela quando o backend
final é `groq`); o ambiente entra quando esse campo está vazio, como no provedor de resumo.

`fireball start --backend groq` é rejeitado na CLI (não está na lista de backends ao vivo).
Testado com uma chamada de API real: em áudio majoritariamente silencioso, o modelo alucinou
a mesma frase curta 3x, exatamente a cada 30s (janela interna do Whisper), com métricas de
confiança "boas" — por isso `GroqBackend` filtra por energia (RMS) do trecho de áudio
correspondente a cada segmento, não só pelas métricas do próprio modelo. O corte por VAD
acima cobre o mesmo caso mais cedo (o silêncio nem é enviado); o filtro fica como rede para
quem chamar o backend direto, sem passar pelo `finalize`.

## Captura de áudio real (`--real`)

Usa `parecord`/`pactl` (PulseAudio/PipeWire) diretamente — testado nesta máquina, é mais
confiável que bibliotecas Python de áudio (PortAudio/sounddevice só expõe dispositivos
agregados genéricos no Linux, não cada fonte individual como o monitor de saída).

Requisitos: `parecord` e `pactl` no PATH (pacote geralmente chamado `pulseaudio-utils` ou
`libpulse`, já presentes em qualquer sistema com PipeWire ou PulseAudio rodando).

```bash
fireball devices                                    # lista as fontes disponíveis
fireball start --name "Reunião" --real              # usa @DEFAULT_SOURCE@ / @DEFAULT_MONITOR@
fireball start --name "Reunião" --real \
  --mic-device alsa_input.pci-....HiFi__Headset__source \
  --system-device alsa_output.pci-....HiFi__Speaker__sink.monitor
```

Cada reunião real grava:

- `mic.pcm` / `mic.wav` — só a sua voz
- `system.pcm` / `system.wav` — o que está tocando no computador (os outros participantes)
- `meeting.wav` — mixagem simples dos dois (soma com clipping)
- `audio_tracks.json` — samplerate/canais/device usados em cada track
- `audio_warnings.log` — só aparece se o áudio do sistema não pôde ser capturado (nesse
  caso a reunião segue gravando só o microfone)

Se o nome do device passado em `--mic-device`/`--system-device` não existir, o comando falha
alto (para mic) ou cai para só-microfone com aviso (para o sistema) — `parecord` sozinho
aceitaria silenciosamente um nome errado e gravaria a fonte padrão sem avisar, então
validamos o nome contra `pactl` antes de gravar.

## GUI + bandeja (`fireball-gui`)

Bandeja e janela moram **dentro do processo do daemon** (`fireball/gui/shell.py`): quando há
sessão gráfica, o loop principal do daemon *é* o loop do Qt. É isso que amarra uma coisa à
outra — ligar o Fireball acende o ícone, desligar apaga.

```bash
pip install -e '.[gui]'
fireball-gui              # garante o daemon de pé e traz a janela pra frente
```

`fireball-gui` não desenha nada: ele sobe o daemon (o que já faz o ícone aparecer) e pede a
janela pelo socket. Rodar duas vezes não abre duas janelas nem dois ícones — só traz a existente
pra frente, como se espera de um app de bandeja.

A bandeja tem status em três estados (ocioso, gravando, parando) e menu (abrir janela, parar
reunião atual, sair). Fechar a janela só esconde — o Fireball continua ligado, e continua na
bandeja.

### A janela

A **barra lateral** é a navegação, e está em toda tela. Ela tem, de cima pra baixo: o botão de
nova reunião, o cartão da reunião **em andamento** (cronômetro e "parar"), a busca, o histórico
agrupado por Hoje / Ontem / Esta semana / Este mês / Mais antigas, e a configuração no pé. A
reunião gravando fica sempre à vista, inclusive enquanto se mexe na configuração: é a única
coisa na tela que exige ação, e escondê-la atrás de um "voltar" seria esconder justamente ela.

Abrir uma reunião dá três abas:

- **Transcrição** — o chat: uma bolha por segmento, agrupadas por falante, com hora. "Você" (o
  microfone daqui) fica à direita, no vermelho da marca; "Outros participantes" (o monitor do
  sistema) à esquerda, no azul. Ao vivo, o rodapé conta o que o pipeline está fazendo (há
  quanto tempo veio a última fala, quantos segmentos). Numa reunião encerrada que tenha sido
  finalizada, um seletor troca entre a transcrição final e a do tempo real.
- **Notas** — o `notes.md` num editor de markdown, salvo sozinho depois que você para de
  digitar (e na hora de sair da aba, que o debounce sozinho perderia as últimas teclas). A
  barra de cima insere markdown de verdade no texto; o arquivo é markdown, e é o mesmo que a
  CLI e o Claude leem e escrevem.
- **Resumo** — a ficha (nome, tags, dia e horário, participantes, abrir a pasta) e o
  **resumo gerado da transcrição**, com *Gerar*/*Regerar*. Nome e tags saem do mesmo pedido
  que escreve o resumo; clicar no nome renomeia à mão. Ver a seção própria abaixo.

Falhas que não interromperam a gravação aparecem como aviso âmbar acima das abas, em
qualquer uma delas: o engine grava `audio_warnings.log` quando só o microfone entrou, e
esse é justamente o erro que passa despercebido — nada quebra na hora, e a falta só
aparece quando alguém vai ler a transcrição e metade da conversa não está lá.

### Ouvir e corrigir

Numa gravação encerrada, o rodapé da aba Transcrição vira o **player** do `meeting.wav`:
tocar/pausar, arrastar a linha do tempo, velocidade, e *Seguir a transcrição*, que destaca a
bolha correspondente ao instante que está tocando e a traz para a tela. Cada bolha ganha,
no hover, **▶ ouvir** (pula o áudio para aquele ponto) e **✎ editar**.

A transcrição guarda deslocamento em segundos (`start`/`end`) desde o tempo real, então o
player acompanha ela em qualquer momento. No tempo real a posição vem do VAD: o `Endpointer` conta quantas
amostras da track já saíram do buffer, e a posição de cada fala é essa contagem — ou seja, a
posição real dentro do PCM, não uma estimativa de relógio. (Conferido contra o whisper na
mesma gravação: a primeira fala dá `1.17` nos dois.) Reuniões gravadas antes disso só têm
posição depois de retranscritas; a janela decide pelo que os segmentos trazem, então elas
continuam funcionando de qualquer forma.

O áudio é o `meeting.wav`, escrito pelo **engine ao terminar de gravar** (mistura de mic +
sistema). Quando o monitor do sistema não abriu, ele não é produzido e a janela cai no
`mic.wav` — ouvir só o seu lado é melhor que não ouvir nada. Sem nenhum dos dois, o rodapé
diz isso em vez de oferecer um controle morto.

Editar corrige **só a transcrição**: o áudio não muda, e o segmento fica marcado com
`edited` no ndjson (e "editado" na bolha). Sem essa marca, uma linha revisada seria
indistinguível do que o motor de fato ouviu. **Excluir** uma fala (✕) tira ela de vez, com
uma confirmação na própria bolha — as ações ficam a um hover de distância de qualquer fala, e
sem o segundo passo um clique torto apagaria a errada. Os `seq` das demais **não** são
renumerados: eles são a identidade de cada fala, e a janela pede "o que veio depois do seq N"
a cada volta do polling; buraco na sequência é esperado.

As duas escritas reescrevem o arquivo num temporário e trocam por cima, porque outros
processos leem esse ndjson e uma troca parcial o deixaria ilegível no meio da leitura.
Enquanto a reunião grava, o daemon recusa as duas — o motor ainda está dando append ali.

### Uma transcrição por reunião

A reunião tem **uma** transcrição (`transcript.ndjson`). Ela nasce do tempo real e o
`finalize` a reescreve por cima com a versão feita sobre o áudio inteiro — que é a mesma
transcrição, melhor. Por isso o botão vira *Retranscrever* depois da primeira vez.

Antes eram dois arquivos convivendo, com um seletor na tela. Duas versões do mesmo texto
significavam ter de escolher em qual corrigir uma frase, e a correção feita numa sumia quando
a outra virava a exibida. Uma só remove a pergunta.

Duas salvaguardas: a troca é atômica (arquivo temporário e `replace`), e uma passada que
devolve zero segmento **não** substitui nada — um backend que falhou em silêncio não pode
apagar o único registro da reunião. Reuniões gravadas no formato antigo são convertidas na
subida do daemon: a final vira a transcrição, e a do tempo real sai de cena.

No cabeçalho ficam o cronômetro e **Parar** enquanto grava; depois, **Finalizar** (ou
**Retranscrever**, se já houver transcrição final), que roda o motor sobre o áudio inteiro sem
travar a janela — o status vira "finalizando" e a tela acompanha pelo polling.

O chat é **incremental**: a cada volta do polling a janela pede só os segmentos com `seq` maior
que o último desenhado (`Api.transcript`) e dá append no DOM, em vez de redesenhar a conversa —
redesenhar jogaria fora a rolagem a cada segundo. E a rolagem só acompanha o fim se você já
estiver no fim: quem subiu pra reler não é arrastado de volta quando chega fala nova.

Uma reunião que começa — pela janela, pela CLI ou por outra tela — abre sozinha no chat, uma vez
só: quem foi olhar outra coisa de propósito não é jogado de volta pra conversa a cada polling.
Quando ela termina (pelo botão, pela bandeja ou pela CLI), a mesma tela drena os últimos
segmentos que o engine escreveu depois do sinal e vira reunião encerrada no lugar.

### Resumo, nome e tags

Quando a gravação termina, um **provedor plugável** lê a transcrição e devolve três coisas de
uma vez: o **nome** da reunião, as **tags** e o **resumo** em markdown. Mesma forma dos backends
de transcrição, em `fireball/summarizers/`; trocar quem escreve não mexe em nada fora daquele
pacote.

São três coisas num pedido só de propósito: as três saem da mesma leitura da transcrição, e
pedi-las separadamente seria pagar a transcrição inteira três vezes — em plano, em token ou em
minuto de modelo local. Por isso a resposta vem em JSON (`title`, `tags`, `summary`), e não em
markdown puro. O preço é aguentar modelo que não obedece formato: a leitura da resposta aceita
cerca de código em volta, texto antes e depois, quebra de linha crua dentro de string, e cai
para "isto aqui é o resumo inteiro" quando não acha JSON nenhum — perder o nome e as tags é bem
melhor que perder o resumo.

**Reunião nasce sem nome.** O nome bom de uma reunião só existe depois dela, e quem está
entrando numa chamada não quer parar para batizá-la; então o campo na tela de início é opcional
e o provedor escreve o nome a partir do que foi dito. Quem já sabe o nome digita — e aí a IA não
mexe: `name_source` guarda se o nome veio de gente (`user`), da agenda (`calendar`) ou de
modelo (`ai`), e qualquer fonte que não seja `ai` fica protegida. As tags, ao contrário, são
substituídas a cada geração: como o resumo, elas são derivadas da transcrição, e manter tag de
uma versão antiga ao lado das novas mostraria duas leituras da mesma reunião como se fossem uma.

#### Os provedores

| provedor | o que é | o que precisa |
| --- | --- | --- |
| `claude_code` | o Claude Code instalado na máquina | `claude` no PATH, com login feito |
| `openai_api` | qualquer API compatível com OpenAI | URL, chave e id do modelo |

`claude_code` foi o primeiro porque não pede chave nem configuração — quem usa o Fireball para
tomar nota com o Claude já tem o `claude` autenticado, e o custo é do plano de quem roda. Ele é
invocado com `--output-format json`, e não com texto puro: em modo texto o Claude Code sai com
código 0 mesmo falhando ("Not logged in · Please run /login" vira stdout como se fosse resposta),
e só o JSON traz o `is_error` que permite saber a verdade. Roda sem ferramentas: aqui é
transformação de texto, não uma sessão de trabalho.

`openai_api` não é "o provedor da OpenAI": é um provedor **sem dono**. "Compatível com OpenAI"
virou o protocolo comum de fato — a própria OpenAI, OpenRouter, Groq, Together, Ollama,
llama.cpp, vLLM e LM Studio falam `POST /v1/chat/completions` com o mesmo corpo — então quem
escolhe de quem é o modelo é a configuração, com três campos: URL, chave e id do modelo. A
chamada é feita com `urllib` da biblioteca padrão, sem SDK: é um JSON de ida e um de volta, e um
SDK aqui seria uma dependência a mais para instalar e para quebrar, amarrando o Fireball ao
dialeto de um fornecedor justamente no lugar cujo objetivo é não ter fornecedor.

Duas coisas que essa liberdade custa, e como elas são pagas. O pedido vai com
`response_format: json_object`, que nem todo servidor compatível aceita — uma recusa 4xx faz o
pedido ser repetido uma vez sem o campo, e o prompt já pede JSON por escrito. E o erro que volta
é traduzido para a frase que diz o que fazer: 401 fala da chave, 404 fala da URL e do modelo,
5xx e 429 dizem que o problema é do outro lado e não da configuração.

A requisição manda um `User-Agent` nosso. Sem ele o urllib se identifica como
`Python-urllib/3.x`, e provedor atrás de Cloudflare responde **403 `error code: 1010`** —
bloqueio pela assinatura do cliente, antes de a chave sequer ser olhada. Como 403 é o mesmo
status de "chave recusada", isso mandava a pessoa trocar uma chave que estava certa; por isso
um 403 cujo corpo não é o JSON de erro da API agora diz que quem barrou foi o serviço na frente
dela, não a credencial.

```bash
fireball summarize <meeting_id>              # provedor e prompt configurados
fireball summarize <meeting_id> --provider openai_api
```

#### Prompts

O pedido que vai para o provedor tem três partes, e **só a do meio se edita**:

```
HEAD   de onde vem a transcrição, o que significam os dois falantes,
       e o JSON com title/tags/summary          ← fixo
─────────────────────────────────────────────────────────────────
       o que o resumo deve dizer e em que forma ← o prompt
─────────────────────────────────────────────────────────────────
TAIL   idioma, regras de título e tag, "não invente nada"  ← fixo
```

O contrato ficar fora do alcance de quem escreve prompt não é zelo: é o que garante que
**todo** prompt continue rendendo nome e tags. Um prompt livre que esquecesse de pedir o JSON
faria a reunião parar de ganhar nome sozinha, e o sintoma apareceria longe da causa — na lista
cheia de "Sem nome", não na tela onde o texto foi escrito.

Já vêm sete, e não um: uma tela de prompts que começa com um item só não ensina o que dá pra
fazer com ela. **Padrão** (parágrafo + Decisões + Em aberto), **Ata formal** (pauta,
deliberações, encaminhamentos — e o que foi decidido *não* fazer), **Só as ações** (checklist,
sem contexto, e que se recusa a transformar "seria bom…" em tarefa), **Resumo executivo**
(quatro linhas para quem faltou), **Conversa 1:1** (temas, combinados, retomar na próxima —
preservando como a pessoa descreveu o que sente), **Entrevista / discovery** (dores, pedidos e
citações literais, sem propor solução) e **Aula ou palestra**, que separa o conteúdo do vídeo
dos comentários de quem assistia — o áudio do sistema e o microfone são justamente duas trilhas
diferentes, e é isso que faz a gravação valer depois.

A lista mora em `~/.fireball/prompts.json`, separada do `settings.json` porque é um punhado de
textos de várias linhas, e não escalares — misturar os dois tornaria a configuração ilegível
justamente onde editar na mão é o caminho (servidor, ssh). Qual deles é o padrão, esse sim é
configuração (`summary_prompt`). O arquivo pode nem existir: sem ele a lista é a dos embutidos, e o
Fireball resume sem ninguém ter salvo nada. Apagar um embutido escreve o arquivo, e a partir
daí é ele que manda — o apagado não volta.

A tela de configuração administra a lista (criar, editar, apagar, e um botão que devolve o
texto embutido); o cabeçalho da aba Resumo tem o seletor de com qual gerar. Duas salvaguardas:
o último prompt não pode ser apagado, e apagar o que era padrão faz o daemon eleger outro —
configuração apontando para um prompt que não existe mais é estado quebrado, e arrumá-lo é
trabalho de quem apagou.

Editar abre um **`<dialog>`**, e não um campo que cresce dentro do cartão: um editor de texto
de dezesseis linhas empurrando o resto da configuração para baixo faz perder o lugar duas vezes
— ao abrir e ao fechar. O elemento nativo dá o Esc, a camada de cima e o fundo escurecido de
graça; clicar no fundo também fecha. (Isso não contradiz o botão de excluir reunião, que evita
diálogo de propósito: lá o que se recusa é o `confirm()` **do sistema**, que a janela do
pywebview não desenha bem. Este é desenhado pela página.) Erro ao salvar aparece dentro do
próprio diálogo — no alerta da tela, ficaria atrás dele.

**A reunião lembra com qual prompt foi resumida** (`summary_prompt` no `meeting.json`), e essa
memória vem antes do padrão. Regerar e o resumo automático que roda depois do `finalize` não
passam por escolha de ninguém; sem ela, a reunião trocaria de formato sozinha no meio do
caminho só porque o padrão da configuração era outro. Prompt apagado depois disso não quebra
nada: o resumo cai no primeiro da lista, porque resumir com outro é melhor que não resumir.

```bash
fireball prompts                              # a lista salva, e qual é o padrão
fireball summarize <meeting_id> --prompt ata  # gerar com um específico
```

Administrar a lista é da janela, não da CLI — mesma regra do resto da configuração, que
também não tem comando.

#### Quando roda

Sozinho, quando a gravação termina e de novo quando a transcrição final fica pronta (o
`finalize` reescreve a transcrição, e o resumo antigo passa a ser sobre um texto que não existe
mais). É o que faz uma reunião sem nome ganhar um sem ninguém pedir: nomear só no clique de
*Gerar* deixaria o histórico cheio de "Sem nome" até alguém lembrar — e ninguém lembra.
`auto_summarize: false` desliga isso e devolve tudo ao botão.

Reunião simulada (`--fake`) **nunca** gera sozinha, e reunião sem nenhuma fala também não: o
motor simulado existe para exercitar o pipeline sem chave de API, e um roteiro de teste virando
chamada paga contraria justamente isso.

Como o finalize, roda em **processo separado supervisionado pelo daemon** — pode demorar
minutos e falhar de fora (sem login, sem rede), e nada disso pode parar o dono do estado. O
processo filho só escreve `summary.md` e `summary_result.json`; quem aplica o nome e as tags no
`meeting.json` é o daemon, quando vê o job sair com código 0 — mesma regra de sempre, um dono só
do estado. O estado da geração mora em `summary_status` e **não** toca no `status` da reunião:
resumo não é etapa do ciclo de vida, e uma reunião finalizada continua finalizada se o resumo
falhar. Regerar apaga o anterior antes de começar — mostrar o resumo velho como se fosse o novo
seria mentir.

O markdown que volta é renderizado montando nós do DOM, nunca `innerHTML`: o texto vem de um
modelo de linguagem sobre uma transcrição, ou seja, de fora, e concatenar isso em HTML
deixaria a transcrição escrever marcação na janela.

### Agenda

A home tem uma lista **Próximas na agenda**. Com um provedor configurado, cada linha é um
evento dos próximos ~24h; clicar inicia a reunião já com nome, local, descrição e convidados
gravados no `meeting.json` (chave aninhada `event`, `name_source: "calendar"`). Isso também
preenche Local / Descrição / Participantes na aba Resumo, e o contexto do evento entra na
parte fixa do pedido de resumo — a pauta que quem marcou já digitou, antes da conversa
começar.

Como transcrição e resumo, a agenda é **plugável** (`fireball/calendars/`). O provedor de
hoje é o `gog`: shell-out no [gogcli](https://github.com/steipete/gogcli) já autenticado na
máquina — mesma jogada do `claude_code`, zero segredo no Fireball. Sem provedor (padrão), a
home mostra o estado vazio e nenhum binário é chamado.

O daemon cacheia a resposta em `~/.fireball/agenda.json` (TTL ~2 min, permissão 0600) e faz
stale-while-revalidate numa thread: o poll de 2s da GUI não reconsulta o calendário a cada
tick. CLI: `fireball agenda [--refresh]` e `fireball start --event <id>`.

Fora desta fatia (de propósito): vincular reunião já em andamento a um evento, escrever de
volta no Google, e iniciar gravação sozinho quando o evento começa.

### Configuração

Backend é decisão de configuração, não de cada reunião: quem vai gravar quer clicar em
"iniciar", não escolher motor de transcrição. Então a janela pergunta só o nome — e nem isso é
obrigatório, já que a IA escreve um depois —, e backend/idioma/agenda ficam na tela de
configuração (⚙), em `~/.fireball/settings.json`.

A tela tem **três abas**, e a divisão é a das partes do Fireball: **Transcrição** (motor ao
vivo, motor final, chave da Groq, idioma), **Resumo** (provedor, chaves, prompts) e **Agenda**
(provedor de calendário e a conta dele). O idioma fica na primeira porque é o que o motor
espera *ouvir* — o resumo sai em português de qualquer jeito, e isso está na parte fixa do
pedido, não no prompt. O **Salvar é um só**, fora das abas: esconder uma aba não apaga o que
está nos campos dela.

| chave | o que é | padrão |
| --- | --- | --- |
| `realtime_backend` | motor da transcrição ao vivo (a que alimenta o chat) | `whisper` |
| `transcribe_live` | transcrever durante a reunião, ou só gravar o áudio | `true` |
| `final_backend` | motor da transcrição final, sobre o áudio inteiro | `whisper` |
| `groq_api_key` | chave da Groq, usada só pelo backend `groq` | vazio |
| `language` | idioma esperado da fala | `pt` |
| `summary_provider` | quem escreve nome, tags e resumo a partir da transcrição | `claude_code` |
| `summary_prompt` | qual prompt salvo o resumo usa quando ninguém escolhe | `padrao` |
| `auto_summarize` | gerar as três coisas sozinho quando a reunião termina | `true` |
| `openai_base_url` | URL da API compatível com OpenAI (provedor `openai_api`) | vazio |
| `openai_api_key` | chave dessa API | vazio |
| `openai_model` | id do modelo nela | vazio |
| `calendar_provider` | quem lê a agenda (`gog`, ou vazio = desligado) | vazio |
| `gog_account` | e-mail passado ao gog como `--account` | vazio |

Os dois modos são configurados separado de propósito: é comum querer um motor local durante a
reunião e `groq` no final. `groq` só aparece na transcrição final — em tempo real seria uma
chamada de API paga a cada poucos segundos.

Como o arquivo pode guardar a chave da API, ele é criado com permissão 600 — e nasce assim, em
vez de virar 600 depois de escrito, o que deixaria uma janela com o segredo no disco sob o umask
de todo mundo. Quem prefere não ter segredo em JSON deixa os campos vazios e usa
`FIREBALL_OPENAI_BASE_URL`, `FIREBALL_OPENAI_API_KEY` e `FIREBALL_OPENAI_MODEL` no ambiente: a
configuração vem primeiro, o ambiente entra quando o campo está vazio.

Reunião começada pela janela é sempre gravação real (mic + sistema). O motor simulado existe
pra testar o pipeline sem microfone — isso é trabalho de desenvolvimento, e mora na CLI
(`fireball start --fake`), não num campo que quem abriu a janela pra gravar precise entender. A
reunião fake aparece na janela igual a qualquer outra enquanto roda.

**Quem resolve a configuração é o daemon**, não o cliente: `start` e `finalize` usam o valor
configurado quando ninguém passa um explícito. Então a mesma configuração vale para a CLI, e as
flags (`--backend`, `--language`, `--no-transcribe`) viram override pontual dela. Configuração
estragada não derruba nada — valor inválido cai no padrão na leitura, mas é recusado com erro
na hora de salvar. Num daemon sem casca gráfica (servidor, ssh), o jeito de mudar é editar o
JSON na mão.

**"Sair" desliga o Fireball inteiro**: para a reunião ativa, fecha os arquivos direito e encerra
o daemon — nada continua gravando sem ninguém olhando, e nenhum ícone fica para trás. Parar a
reunião pela bandeja não trava a interface: o engine ainda leva um instante fechando os `.wav`,
e o ícone mostra "parando" nesse meio-tempo.

O extra `[gui]` continua opcional: sem ele (ou sem display) o daemon roda sem casca e só a CLI
opera — quem usa o Fireball num servidor não precisa instalar PyQt6.

**Um toolkit só: Qt.** Janela via `pywebview` (backend Qt/PyQt6) e bandeja via
`QSystemTrayIcon` (parte do PyQt6, sem lib extra) — de propósito, não GTK/`pystray`. Em teste
manual, rodar o backend AppIndicator do `pystray` (GTK/GLib) no mesmo processo que o Qt do
pywebview travava com erros de contexto de thread do Qt/OpenGL. Um toolkit, um processo, um
event loop.

**Pegadinha real do pywebview**: `QApplication.setQuitOnLastWindowClosed(False)` **não**
impede o app de fechar quando a janela fecha — o próprio pywebview chama `_app.exit()`
explicitamente dentro do `closeEvent` quando a última janela dele fecha
(`webview/platforms/qt.py`), ignorando esse ajuste do Qt. A forma que funciona é interceptar
`window.events.closing` e cancelar o close de verdade (retornar `False`), escondendo a janela
no lugar — ver `fireball/gui/shell.py:on_closing`.

**Ruído conhecido no desligamento**: o Qt imprime `Release of profile requested but
WebEnginePage still not deleted. Expect troubles!` toda vez que o daemon com casca desliga. É a
ordem de destruição interna do QtWebEngine com o pywebview, no fim do processo; destruir a
janela antes de encerrar o loop (na mesma volta e na seguinte) não muda nada. O desligamento em
si é limpo — reunião parada, `.wav` fechados, socket removido, nenhum processo vazando.

No Linux, `pywebview` com Qt precisa do `qtpy`+`PyQt6`+`PyQt6-WebEngine` (tudo isolado no
venv, sem pacote de sistema); a alternativa seria GTK+`webkit2gtk`, que normalmente exige
instalar um pacote de sistema.

## Instalar

```bash
cd /home/rafaelt/Desktop/fireball
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usar as skills no Claude Code

Duas skills neste repo:

- `skills/fireball` — genérica, escrivão de reunião independente de vault.
- `skills/fireball-meeting` — mesma coisa, mas escreve as notas ao vivo direto numa nota
  `Meeting`/`Transcript` de um vault Tolaria (em vez do `notes.md` do Fireball). Pressupõe que
  o vault já tem os tipos `Meeting`/`Transcript` definidos.

Copie (ou dê symlink em) cada uma para dentro de `~/.claude/skills/`:

```bash
ln -s /home/rafaelt/Desktop/fireball/skills/fireball ~/.claude/skills/fireball
ln -s /home/rafaelt/Desktop/fireball/skills/fireball-meeting ~/.claude/skills/fireball-meeting
```

## Testar o fluxo manualmente

```bash
fireball start --interval 1     # sem --name: a reunião nasce sem nome
# guarde o meeting_id retornado

fireball transcript follow <meeting_id>   # roda em foreground, uma linha por segmento novo

fireball note <meeting_id> "Decisão: usar Monitor para o loop ao vivo"

fireball status <meeting_id>
fireball stop                                         # sem argumento: para a reunião ativa
fireball finalize <meeting_id>                        # --backend whisper|parakeet, padrão: o mesmo do start
fireball transcript show <meeting_id>

fireball summarize <meeting_id>                       # nome + tags + resumo (roda sozinho ao fim, exceto em --fake)
fireball rename <meeting_id> "Nome que eu escolhi"    # nome dado à mão não é trocado pela IA
```

## Onde ficam os dados

`~/.fireball/meetings/<meeting_id>/` (ou `$FIREBALL_HOME/meetings/...` se a variável de ambiente
estiver definida). Ver `skills/fireball/SKILL.md` para o layout de arquivos.

Ao lado deles, na raiz do `$FIREBALL_HOME`: `settings.json` (as preferências, com permissão
600), `prompts.json` (os prompts de resumo), `daemon.sock`, `daemon.lock` e `daemon.log`.
