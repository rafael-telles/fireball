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
  inteiro (mais preciso que o tempo real) e mescla mic/system por ordem de início.
- Skill (`skills/fireball/SKILL.md`) com as instruções de como o Claude deve agir como escrivão.

O que **não** está implementado ainda:

- Transcrição final via provedor em nuvem (Groq/OpenAI) — hoje "final" também é local,
  pelo mesmo backend (whisper/parakeet) usado ao vivo, só que sobre o áudio inteiro.
- Diarização de verdade para "Outros participantes" (hoje é só *um* falante genérico —
  o monitor do sistema não distingue quem está falando do outro lado).
- GUI.

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

## Instalar

```bash
cd /home/rafaelt/Desktop/fireball
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usar a skill no Claude Code

Copie (ou dê symlink em) `skills/fireball` para dentro de `~/.claude/skills/fireball`:

```bash
ln -s /home/rafaelt/Desktop/fireball/skills/fireball ~/.claude/skills/fireball
```

## Testar o fluxo manualmente

```bash
fireball start --name "Reunião de teste" --interval 1
# guarde o meeting_id retornado

fireball transcript follow <meeting_id>   # roda em foreground, uma linha por segmento novo

fireball note <meeting_id> "Decisão: usar Monitor para o loop ao vivo" 
fireball action add <meeting_id> --title "Abrir ticket no Linear" --system linear

fireball status <meeting_id>
fireball stop <meeting_id>
fireball finalize <meeting_id>                        # --backend whisper|parakeet, padrão: o mesmo do start
fireball transcript show <meeting_id> --source final
```

## Onde ficam os dados

`~/.fireball/meetings/<meeting_id>/` (ou `$FIREBALL_HOME/meetings/...` se a variável de ambiente
estiver definida). Ver `skills/fireball/SKILL.md` para o layout de arquivos.
