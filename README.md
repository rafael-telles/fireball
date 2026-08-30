# Fireball

Esboço do pivô do Fireball para skill + tools do Claude Code. Ver a nota de projeto no vault
Tolaria (`fireball.md`) para o desenho completo; este repositório é a primeira fatia executável:
o CLI e a skill.

## Status

Sketch inicial. O que funciona:

- CLI completo (`fireball`) com dois motores:
  - `--fake` (padrão): transcrição **simulada**, para exercitar todo o fluxo (gravação →
    transcrição ao vivo → notas → ações pendentes → transcrição final) sem depender de
    microfone ou chaves de API.
  - `--real`: captura **de verdade** o microfone e o áudio do sistema (o que os outros
    participantes da reunião estão falando, via monitor da saída padrão), gravando
    `mic.wav`, `system.wav` e uma mixagem `meeting.wav` por reunião — **e transcreve em
    tempo real de verdade**, local, sem chave de API (ver Backends de transcrição abaixo).
- Transcrição final (`fireball finalize`) roda o backend escolhido sobre cada `.wav`
  inteiro (mais preciso que o tempo real) e mescla mic/system por ordem de início — local
  (whisper/parakeet) ou via API da Groq (`--backend groq`, só pra transcrição final).
- Skill (`skills/fireball/SKILL.md`) com as instruções de como o Claude deve agir como escrivão.
- **Daemon** (`fireball.daemon`) — o processo dono do estado, que supervisiona as gravações,
  garante uma reunião por vez e **mostra o ícone na bandeja**: enquanto o Fireball está ligado,
  ele aparece lá. Ver seção própria abaixo.
- GUI + bandeja — iniciar/parar reunião, transcrição ao vivo em formato de chat e histórico
  de reuniões passadas, minimizado pra bandeja. `fireball-gui` abre a janela do daemon. Ver
  seção própria abaixo.

O que **não** está implementado ainda:

- Diarização de verdade para "Outros participantes" (hoje é só *um* falante genérico —
  o monitor do sistema não distingue quem está falando do outro lado).
- Na GUI: notas e ações na tela da reunião (hoje só a transcrição aparece), overlay de
  áudio, finalizar uma reunião pela janela (ver plano em `.claude/plans/` da sessão que
  criou isso).

## Daemon

O Fireball tem **um processo dono do estado**, e ele é o mesmo processo que mostra o ícone na
bandeja. **Enquanto o Fireball está ligado, ele aparece na bandeja** — não existe daemon rodando
sem ícone nem ícone sem daemon. O ícone é a resposta visual à pergunta "isso está ligado?".

```
                          ┌───────────── daemon ─────────────┐
fireball (CLI) ─ socket ─→│  estado (reunião ativa, lock)     │─→ engine (gravação/transcrição)
                          │  bandeja + janela (Qt)            │─→ finalize (transcrição final)
fireball-gui  ─ socket ─→ │  meeting.json, notes.md, actions  │
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

Fora do caminho feliz: `crashed` (o engine morreu sozinho), `failed` (o engine nem subiu),
`finalize_failed`. Os três primeiros mais `stopping` contam como "viva" — é o que `transcript
follow` usa pra saber quando parar.

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

### `groq` — transcrição final via API (só para `finalize`)

Terceiro backend, **só para lote** (`BatchBackend`, não implementa tempo real — é uma API
paga por requisição, chamar por "fala" no loop ao vivo geraria uma requisição a cada poucos
segundos). Usa `whisper-large-v3-turbo` hospedado pela Groq.

```bash
pip install -e '.[groq]'
export GROQ_API_KEY=...   # https://console.groq.com/keys
fireball finalize <meeting_id> --backend groq
```

`fireball start --backend groq` é rejeitado na CLI (não está na lista de backends ao vivo).
Testado com uma chamada de API real: em áudio majoritariamente silencioso, o modelo alucinou
a mesma frase curta 3x, exatamente a cada 30s (janela interna do Whisper), com métricas de
confiança "boas" — por isso `GroqBackend` filtra por energia (RMS) do trecho de áudio
correspondente a cada segmento, não só pelas métricas do próprio modelo.

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

A janela tem duas telas:

- **Início** — formulário de nova reunião (só o nome; backend e idioma ficam na *Configuração*,
  abaixo) e o **histórico**: uma linha por reunião, mais recentes primeiro, com data, duração,
  quantos segmentos, se já tem transcrição final e o status. Clicar abre a reunião.
- **Configuração** (⚙ no topo) — backend da transcrição ao vivo, backend da transcrição final
  e idioma.
- **Reunião** — a transcrição em **formato de chat**: uma bolha por segmento, agrupadas por
  falante, com hora. "Você" (o microfone daqui) fica à direita; o áudio do sistema, à esquerda.
  Ao vivo, o cabeçalho mostra o cronômetro e o botão de parar; numa reunião já encerrada que
  tenha sido finalizada, um seletor troca entre a transcrição final e a do tempo real.

O chat é **incremental**: a cada volta do polling a janela pede só os segmentos com `seq` maior
que o último desenhado (`Api.transcript`) e dá append no DOM, em vez de redesenhar a conversa —
redesenhar jogaria fora a rolagem a cada segundo. E a rolagem só acompanha o fim se você já
estiver no fim: quem subiu pra reler não é arrastado de volta quando chega fala nova.

Uma reunião que começa — pela janela, pela CLI ou por outra tela — abre sozinha no chat, uma vez
só: quem voltou pro histórico de propósito não é jogado de volta pra conversa a cada polling.
Quando ela termina (pelo botão, pela bandeja ou pela CLI), a mesma tela drena os últimos
segmentos que o engine escreveu depois do sinal e vira histórico no lugar.

### Configuração

Backend é decisão de configuração, não de cada reunião: quem vai gravar quer clicar em
"iniciar", não escolher motor de transcrição. Então a janela pergunta só o nome, e
backend/idioma ficam na tela de configuração (⚙), em `~/.fireball/settings.json`:

| chave | o que é | padrão |
| --- | --- | --- |
| `realtime_backend` | motor da transcrição ao vivo (a que alimenta o chat) | `whisper` |
| `transcribe_live` | transcrever durante a reunião, ou só gravar o áudio | `true` |
| `final_backend` | motor da transcrição final, sobre o áudio inteiro | `whisper` |
| `language` | idioma esperado da fala | `pt` |

Os dois modos são configurados separado de propósito: é comum querer um motor local durante a
reunião e `groq` no final. `groq` só aparece na transcrição final — em tempo real seria uma
chamada de API paga a cada poucos segundos.

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
fireball start --name "Reunião de teste" --interval 1
# guarde o meeting_id retornado

fireball transcript follow <meeting_id>   # roda em foreground, uma linha por segmento novo

fireball note <meeting_id> "Decisão: usar Monitor para o loop ao vivo" 
fireball action add <meeting_id> --title "Abrir ticket no Linear" --system linear

fireball status <meeting_id>
fireball stop                                         # sem argumento: para a reunião ativa
fireball finalize <meeting_id>                        # --backend whisper|parakeet, padrão: o mesmo do start
fireball transcript show <meeting_id> --source final
```

## Onde ficam os dados

`~/.fireball/meetings/<meeting_id>/` (ou `$FIREBALL_HOME/meetings/...` se a variável de ambiente
estiver definida). Ver `skills/fireball/SKILL.md` para o layout de arquivos.
