# Fireball

Esboço do pivô do Fireball para skill + tools do Claude Code. Ver a nota de projeto no vault
Tolaria (`fireball.md`) para o desenho completo; este repositório é a primeira fatia executável:
o CLI e a skill.

## Status

Sketch inicial. O que funciona:

- CLI completo (`fireball`) com um motor de transcrição **simulado** (`--fake`), para exercitar
  todo o fluxo (gravação → transcrição ao vivo → notas → ações pendentes → transcrição final)
  sem depender de microfone ou chaves de API.
- Skill (`skills/fireball/SKILL.md`) com as instruções de como o Claude deve agir como escrivão.

O que **não** está implementado ainda (stubs com `NotImplementedError` e `TODO` no código):

- Captura de áudio real e motor de transcrição em tempo real real (`fireball/engine.py:run_real_engine`).
- Transcrição final via provedor (Groq/OpenAI) (`fireball/cli.py:finalize`).
- GUI.

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
