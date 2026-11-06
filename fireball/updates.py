"""Atualizar o Fireball a partir das releases publicadas no GitHub.

Num AppImage o app é um arquivo só: atualizar é trocar esse arquivo e deixar o
daemon voltar nele. Este módulo é a parte que dá para exercitar sem daemon e sem
rede de verdade — comparar versões, achar o asset da release, baixar conferindo
o SHA-256 que o GitHub publica e trocar o arquivo de forma atômica.

Sem dependência nova, de propósito: `urllib` da biblioteca padrão. O AppImage
não tem pip, e a única coisa que ele precisa da rede é o HTTPS do github.com.

Quem decide *quando* conferir e atualizar é o `DaemonCore` (`update_check` /
`update_apply`): aqui nada sabe de reunião, de janela nem de processo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

REPO = "rafael-telles/fireball"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_URL = f"https://github.com/{REPO}/releases"

# O asset que o workflow de release publica (ver .github/workflows/build.yml e
# packaging/build-appimage.sh). Qualquer outra coisa na release é ignorada.
ASSET_RE = re.compile(r"^Fireball-(?P<version>[^-]+)-x86_64\.AppImage$")

TIMEOUT_S = 20.0
CHUNK = 1 << 20


class UpdateError(RuntimeError):
    """Falha de conferência ou de atualização, com mensagem para a tela."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version_from_pyproject(path: Path) -> str:
    """A versão declarada no `pyproject.toml`, ou "" quando não dá para ler."""
    try:
        import tomllib

        with open(path, "rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except Exception:  # noqa: BLE001 — sem tomllib (3.10) ou arquivo estranho
        return ""


def current_version() -> str:
    """A versão que está rodando.

    Num checkout vale o `pyproject.toml`: instalação editable
    (`pip install -e .`) guarda no dist-info a versão de quando foi instalada,
    e ela envelhece a cada bump — o dev veria "0.1.0" para sempre e a tela
    ofereceria uma atualização que ele já tem. No AppImage não existe
    pyproject, então vale o metadado do pacote, que é justamente o que o build
    gerou a partir dele.
    """
    declarada = version_from_pyproject(Path(__file__).resolve().parent.parent / "pyproject.toml")
    if declarada:
        return declarada
    try:
        from importlib.metadata import version

        return version("fireball")
    except Exception:  # noqa: BLE001 — pacote sem metadado
        return "0.0.0"


def parse_version(text: object) -> tuple:
    """'v0.2.0' → (0, 2, 0). Sufixo ('0.2.0-rc1') não muda a comparação."""
    return tuple(int(part) for part in re.findall(r"\d+", str(text or "")))


def is_newer(candidate: object, current: object) -> bool:
    return parse_version(candidate) > parse_version(current)


def appimage_path() -> Optional[Path]:
    """O arquivo do AppImage que está rodando, ou None quando roda do fonte.

    `APPIMAGE` é posto pelo AppRun do próprio AppImage. Sem ela não existe
    arquivo único para trocar — atualizar seria baixar um arquivo que ninguém
    executa, então a tela recusa em vez de fingir que atualizou.
    """
    raw = os.environ.get("APPIMAGE")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def version_from_asset(name: str) -> str:
    match = ASSET_RE.match(str(name or ""))
    return match.group("version") if match else ""


def release_info(payload: dict, current: str = "") -> dict:
    """O que interessa do JSON de `/releases/latest`, sem nada de GitHub dentro."""
    if not isinstance(payload, dict):
        raise UpdateError("Resposta inesperada do GitHub (não é um objeto JSON).")
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("A resposta do GitHub não trouxe tag de release.")
    latest = tag[1:] if tag[:1] in ("v", "V") else tag

    asset = None
    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not ASSET_RE.match(name) or not item.get("browser_download_url"):
            continue
        digest = str(item.get("digest") or "")
        asset = {
            "name": name,
            "url": str(item["browser_download_url"]),
            "size": int(item.get("size") or 0),
            # o GitHub publica `sha256:<hex>`; sem isso o download ainda é
            # conferido pelo tamanho, mas a mensagem diz o que não deu para
            # checar em vez de dar por verificado
            "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else "",
        }
        break

    current = current or current_version()
    return {
        "current": current,
        "latest": latest,
        "newer": is_newer(latest, current),
        "tag": tag,
        "page_url": str(payload.get("html_url") or f"{PAGE_URL}/tag/{tag}"),
        "notes": str(payload.get("body") or "").strip(),
        "published_at": str(payload.get("published_at") or ""),
        "asset": asset,
        "checked_at": now_iso(),
    }


def _https_context() -> "ssl.SSLContext":
    """Contexto TLS que não morre junto com o AppImage.

    O AppRun exporta `SSL_CERT_FILE` para o `certs.pem` do bundle. Um daemon
    destacado sobrevive ao processo que montou o AppImage, e aí aquele caminho
    aponta para o nada: o `ssl` fica sem CA nenhuma e todo HTTPS vira
    `CERTIFICATE_VERIFY_FAILED`. Aqui o arquivo é conferido antes de virar
    contexto — se sumiu, valem as CAs do sistema, em vez de a conferência de
    atualização morrer por causa de uma variável de ambiente herdada.
    """
    padrao = ssl.get_default_verify_paths()
    candidatos = (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        padrao.cafile,
        padrao.openssl_cafile,
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/cert.pem",
    )
    for candidato in candidatos:
        if not candidato or not Path(candidato).is_file():
            continue
        try:
            return ssl.create_default_context(cafile=candidato)
        except (ssl.SSLError, OSError):
            continue
    return ssl.create_default_context()


def _urlopen(request, timeout: float = TIMEOUT_S):
    """O `urlopen` padrão, com o contexto TLS do módulo."""
    return urllib.request.urlopen(request, timeout=timeout, context=_https_context())


def fetch_json(url: str, timeout: float = TIMEOUT_S, opener: Optional[Callable] = None) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            # o GitHub recusa requisição sem User-Agent, e a versão no cabeçalho
            # é o que aparece no log deles quando algo sai errado
            "User-Agent": f"fireball/{current_version()}",
        },
    )
    opener = opener or _urlopen
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("O GitHub não tem release publicada do Fireball.") from exc
        raise UpdateError(f"O GitHub respondeu {exc.code} ao procurar a release.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Não consegui falar com o GitHub: {exc}") from exc
    except ValueError as exc:
        raise UpdateError("O GitHub devolveu algo que não é JSON.") from exc


def check(
    current: str = "",
    timeout: float = TIMEOUT_S,
    opener: Optional[Callable] = None,
) -> dict:
    """A release mais nova comparada com a versão que está rodando."""
    return release_info(fetch_json(RELEASES_URL, timeout=timeout, opener=opener), current)


def sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def download(
    asset: dict,
    dest: Path,
    opener: Optional[Callable] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Baixa `asset` para `dest`, conferindo tamanho e SHA-256 antes de trocar.

    Escreve num `.part` ao lado e só faz `os.replace` depois de conferir: o app
    inteiro é esse arquivo, então um download interrompido não pode deixar um
    AppImage pela metade no lugar do que funciona.
    """
    url = str((asset or {}).get("url") or "")
    if not url:
        raise UpdateError("A release não trouxe URL de download do AppImage.")
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    esperado_bytes = int((asset or {}).get("size") or 0)
    esperado_sha = str((asset or {}).get("sha256") or "").lower()

    hasher = hashlib.sha256()
    recebido = 0
    opener = opener or _urlopen
    try:
        with opener(url, timeout=TIMEOUT_S) as resposta, open(part, "wb") as saida:
            while True:
                bloco = resposta.read(CHUNK)
                if not bloco:
                    break
                saida.write(bloco)
                hasher.update(bloco)
                recebido += len(bloco)
                if progress is not None:
                    progress(recebido, esperado_bytes)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Falha baixando o AppImage: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    if esperado_bytes and recebido != esperado_bytes:
        part.unlink(missing_ok=True)
        raise UpdateError(
            f"O AppImage veio com {recebido} bytes e a release diz {esperado_bytes} — não troquei nada."
        )
    if esperado_sha and hasher.hexdigest() != esperado_sha:
        part.unlink(missing_ok=True)
        raise UpdateError("O SHA-256 do download não bate com o da release — não troquei o AppImage.")

    os.chmod(part, 0o755)
    os.replace(part, dest)
    return dest


def apply(
    asset: dict,
    target: Path,
    opener: Optional[Callable] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Troca o AppImage em `target` pelo da release publicada."""
    if not asset:
        raise UpdateError("A release mais nova não publicou AppImage.")
    target = Path(target)
    folder = target.parent
    if not folder.is_dir() or not os.access(folder, os.W_OK):
        raise UpdateError(
            f"Sem permissão de escrita em {folder}: atualize à mão (baixe o AppImage novo da release)."
        )
    download(asset, target, opener=opener, progress=progress)
    return {"path": str(target), "version": version_from_asset(asset.get("name", ""))}
