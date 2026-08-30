"""Servidor local que entrega o áudio das reuniões para a janela.

Por que isto existe: o pywebview **não** carrega a janela por `file://` — ele
sobe um servidor HTTP próprio e serve o `index.html` de
`http://127.0.0.1:<porta>/`. De uma página http, o Chromium recusa mídia em
`file://` ("Media load rejected by URL safety check"), então apontar o
`<audio>` para o caminho do .wav no disco simplesmente não toca. Um teste
carregando a mesma página como `file://` passa — e é justamente por isso que
ele não valia: a diferença entre o teste e o app era exatamente a que
importava.

O áudio então também vai por http, daqui. O servidor:

- escuta só em 127.0.0.1, numa porta efêmera;
- serve apenas os .wav conhecidos de dentro da pasta de reuniões, com o id
  validado contra travessia de caminho;
- exige um token sorteado na subida, para que outra página aberta na máquina
  não consiga varrer o áudio das suas reuniões;
- responde a `Range`, sem o que o Chromium não consegue buscar posição dentro
  do arquivo — e o player existe justamente para acompanhar a transcrição no
  ponto certo.
"""

from __future__ import annotations

import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from fireball import storage

# só o que o engine produz; nada de servir arquivo arbitrário da pasta
SERVABLE = {"meeting.wav", "mic.wav", "system.wav"}

# o id é sempre <timestamp>-<slug>; recusar o resto mata travessia de caminho
# antes de tocar no filesystem
MEETING_ID = re.compile(r"^[A-Za-z0-9._-]+$")

RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class _Handler(BaseHTTPRequestHandler):
    server_version = "fireball-audio"

    def log_message(self, *args) -> None:
        pass  # o log do daemon não ganha nada com uma linha por bloco de áudio

    def _reject(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _resolve(self) -> Optional[Path]:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        # /audio/<token>/<meeting_id>/<arquivo>.wav
        if len(parts) != 4 or parts[0] != "audio":
            return None
        token, meeting_id, name = parts[1], parts[2], parts[3]
        if not secrets.compare_digest(token, self.server.token):
            return None
        if name not in SERVABLE or not MEETING_ID.match(meeting_id) or meeting_id in (".", ".."):
            return None

        root = storage.meetings_root().resolve()
        path = (root / meeting_id / name).resolve()
        # cinto e suspensório: mesmo com o id validado, confirma que o
        # caminho resolvido continua dentro da pasta de reuniões
        if not path.is_file() or root not in path.parents:
            return None
        return path

    def do_HEAD(self) -> None:
        self.do_GET(body=False)

    def do_GET(self, body: bool = True) -> None:
        path = self._resolve()
        if path is None:
            return self._reject(404)

        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False

        match = RANGE.match(self.headers.get("Range", "") or "")
        if match:
            raw_start, raw_end = match.group(1), match.group(2)
            if raw_start:
                start = int(raw_start)
                if raw_end:
                    end = min(int(raw_end), size - 1)
            elif raw_end:
                # "bytes=-N": os N últimos bytes
                start = max(0, size - int(raw_end))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return

        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # o player fechou a conexão (seek, pausa) — normal
                remaining -= len(chunk)


class AudioServer:
    """Sobe em thread própria e devolve a URL base para a janela montar links."""

    def __init__(self) -> None:
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._token = secrets.token_urlsafe(16)

    def start(self) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.token = self._token
        httpd.daemon_threads = True
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        host, port = httpd.server_address[:2]
        return f"http://{host}:{port}/audio/{self._token}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
