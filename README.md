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
    `mic.wav`, `system.wav` e uma mixagem `meeting.wav` por reunião. Ver detalhes abaixo.
- Skill (`skills/fireball/SKILL.md`) com as instruções de como o Claude deve agir como escrivão.

O que **não** está implementado ainda (stubs com `NotImplementedError` e `TODO` no código):

- Transcrição em tempo real de verdade a partir do áudio capturado (rodar um modelo de
  streaming sobre `mic.wav`/`system.wav` e escrever segmentos em `transcript.ndjson` —
  hoje isso só existe no modo `--fake`).
- Transcrição final via provedor (Groq/OpenAI) (`fireball/cli.py:finalize`).
- GUI.

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
fireball finalize <meeting_id>
fireball transcript show <meeting_id> --source final
```

## Onde ficam os dados

`~/.fireball/meetings/<meeting_id>/` (ou `$FIREBALL_HOME/meetings/...` se a variável de ambiente
estiver definida). Ver `skills/fireball/SKILL.md` para o layout de arquivos.
