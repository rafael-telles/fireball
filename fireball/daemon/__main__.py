"""`python -m fireball.daemon` — sobe o daemon em foreground.

É assim que a CLI e a GUI auto-sobem o daemon (destacado, com log em
`$FIREBALL_HOME/daemon.log`). Ver `fireball.daemon.client.ensure_daemon`.
"""

import sys

from dotenv import load_dotenv
from pathlib import Path

# mesmos segredos que a CLI carrega (GROQ_API_KEY etc.) — o daemon é quem
# sobe os processos de engine/finalize, então precisa do ambiente completo.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from fireball.daemon import server

try:
    sys.exit(server.run(tray="--no-tray" not in sys.argv))
except server.AlreadyRunning as exc:
    print(exc, file=sys.stderr)
    sys.exit(0)
