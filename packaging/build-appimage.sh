#!/usr/bin/env bash
# Monta dist/Fireball-<versão>-x86_64.AppImage sobre o Python relocável do
# python-appimage (niess/python-appimage), com todos os extras instalados.
set -euo pipefail

PYTHON_SERIES="${PYTHON_SERIES:-3.12}"
EXTRAS="${EXTRAS:-whisper,parakeet,groq,voice,gui}"
VERSION="${VERSION:-$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT/build/appimage"
APPDIR="$WORK/AppDir"
OUT="$ROOT/dist/Fireball-$VERSION-x86_64.AppImage"

rm -rf "$WORK"
mkdir -p "$WORK" "$ROOT/dist"
cd "$WORK"

python_url="$(curl -fsSL "https://api.github.com/repos/niess/python-appimage/releases/tags/python$PYTHON_SERIES" \
  | python3 -c 'import json, sys
assets = json.load(sys.stdin)["assets"]
print(next(a["browser_download_url"] for a in assets if a["name"].endswith("manylinux_2_28_x86_64.AppImage")))')"
curl -fsSL -o python.AppImage "$python_url"
curl -fsSL -o appimagetool \
  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x python.AppImage appimagetool

./python.AppImage --appimage-extract >/dev/null
mv squashfs-root "$APPDIR"

PY="$APPDIR/opt/python$PYTHON_SERIES/bin/python$PYTHON_SERIES"
"$PY" -m pip install --no-cache-dir --no-warn-script-location "$ROOT[$EXTRAS]"

rm -f "$APPDIR"/AppRun "$APPDIR"/*.desktop "$APPDIR"/*.png "$APPDIR"/.DirIcon
install -m 755 "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
sed -i "s|@PYTHON@|opt/python$PYTHON_SERIES/bin/python$PYTHON_SERIES|" "$APPDIR/AppRun"
sed -e 's|^Exec=.*|Exec=fireball|' -e 's|^Icon=.*|Icon=fireball|' \
  "$ROOT/packaging/fireball.desktop" > "$APPDIR/fireball.desktop"
cp "$ROOT/packaging/fireball.svg" "$APPDIR/fireball.svg"
ln -s fireball.svg "$APPDIR/.DirIcon"

ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 ./appimagetool --no-appstream "$APPDIR" "$OUT"
echo "$OUT"
