#!/bin/bash
set -euo pipefail

ATLAS_VERSION="2.0.0"
PYTHON_VERSION="3.12.10"
PYTHON_ROOT="/Library/Frameworks/Python.framework/Versions/3.12"
PYTHON_BIN="${PYTHON_ROOT}/bin/python3"
PYTHON_PKG="python-${PYTHON_VERSION}-macos11.pkg"
PYTHON_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/${PYTHON_PKG}"

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

echo "============================================================"
echo " ATLAS V${ATLAS_VERSION} - Universal macOS Build"
echo "============================================================"
echo "Repository: $ROOT_DIR"

REQUIRED_FILES=(
  "main.py"
  "launcher_desktop.py"
  "schema.sql"
  "app/templates/index.html"
  "app/templates/update_catalog_popup.html"
  "app/static/style.css"
  "assets/icon.png"
)

for f in "${REQUIRED_FILES[@]}"; do
  if [[ ! -e "$f" ]]; then
    echo "ERROR: Missing required file: $f"
    exit 1
  fi
done

echo "[1/10] Source validation OK."

if ! xcode-select -p >/dev/null 2>&1; then
  echo "[2/10] Apple Command Line Tools are not installed."
  echo "A GUI installer will now be requested. After it finishes, run this script again."
  xcode-select --install || true
  exit 1
fi

echo "[2/10] Command Line Tools: $(xcode-select -p)"
clang --version | head -n 1 || true

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[3/10] Installing python.org universal2 Python ${PYTHON_VERSION}..."
  mkdir -p "$HOME/Downloads"
  cd "$HOME/Downloads"
  curl -fL "$PYTHON_URL" -o "$PYTHON_PKG"
  sudo installer -pkg "$PYTHON_PKG" -target /
  cd "$ROOT_DIR"
fi

"$PYTHON_BIN" --version
PY_ARCHS="$(lipo -archs "$PYTHON_BIN" 2>/dev/null || true)"
echo "Python architectures: ${PY_ARCHS}"

if [[ "$PY_ARCHS" != *"arm64"* || "$PY_ARCHS" != *"x86_64"* ]]; then
  echo "ERROR: Python is not universal2. Expected arm64 + x86_64."
  exit 1
fi

echo "[4/10] Creating requirements-mac.txt..."
cat > requirements-mac.txt <<'EOF'
Flask==3.0.3
waitress==3.0.0
appdirs==1.4.4
Jinja2==3.1.4
MarkupSafe==2.1.5
pywebview==5.1
pyobjc
pyinstaller==6.3.0
EOF

echo "[5/10] Making launcher_desktop.py macOS-aware..."
"$PYTHON_BIN" - <<'PY'
from pathlib import Path

p = Path("launcher_desktop.py")
text = p.read_text(encoding="utf-8")
old = '        webview.start(gui="edgechromium")'
new = '''        if sys.platform == "darwin":
            LOG.info("Starting pywebview with native macOS renderer.")
            webview.start()
        elif os.name == "nt":
            LOG.info("Starting pywebview with Edge Chromium renderer.")
            webview.start(gui="edgechromium")
        else:
            LOG.info("Starting pywebview with platform-default renderer.")
            webview.start()'''

if old in text:
    text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    print("launcher_desktop.py updated for Windows + macOS.")
elif "Starting pywebview with native macOS renderer." in text:
    print("launcher_desktop.py is already platform-aware.")
else:
    print("WARNING: Expected webview.start(gui=\"edgechromium\") line was not found.")
    print("Review launcher_desktop.py before distribution.")
PY

echo "[6/10] Creating assets/ATLAS.icns..."
rm -rf assets/ATLAS.iconset
mkdir -p assets/ATLAS.iconset
sips -z 16 16 assets/icon.png --out assets/ATLAS.iconset/icon_16x16.png >/dev/null
sips -z 32 32 assets/icon.png --out assets/ATLAS.iconset/icon_16x16@2x.png >/dev/null
sips -z 32 32 assets/icon.png --out assets/ATLAS.iconset/icon_32x32.png >/dev/null
sips -z 64 64 assets/icon.png --out assets/ATLAS.iconset/icon_32x32@2x.png >/dev/null
sips -z 128 128 assets/icon.png --out assets/ATLAS.iconset/icon_128x128.png >/dev/null
sips -z 256 256 assets/icon.png --out assets/ATLAS.iconset/icon_128x128@2x.png >/dev/null
sips -z 256 256 assets/icon.png --out assets/ATLAS.iconset/icon_256x256.png >/dev/null
sips -z 512 512 assets/icon.png --out assets/ATLAS.iconset/icon_256x256@2x.png >/dev/null
sips -z 512 512 assets/icon.png --out assets/ATLAS.iconset/icon_512x512.png >/dev/null
sips -z 1024 1024 assets/icon.png --out assets/ATLAS.iconset/icon_512x512@2x.png >/dev/null
iconutil -c icns assets/ATLAS.iconset -o assets/ATLAS.icns

echo "[7/10] Creating build-macos.spec..."
cat > build-macos.spec <<'EOF'
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "schema.sql"), "."),
    (str(ROOT / "app" / "templates"), "app/templates"),
    (str(ROOT / "app" / "static"), "app/static"),
]

hiddenimports = [
    "webview",
    "webview.platforms.cocoa",
    "Cocoa",
    "Quartz",
    "WebKit",
    "Security",
]

a = Analysis(
    ["launcher_desktop.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ATLAS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="universal2",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ATLAS",
)

app = BUNDLE(
    coll,
    name="ATLAS.app",
    icon=str(ROOT / "assets" / "ATLAS.icns"),
    bundle_identifier="com.philips.srcops.atlas",
    version="2.0.0",
    info_plist={
        "CFBundleName": "ATLAS",
        "CFBundleDisplayName": "ATLAS",
        "CFBundleShortVersionString": "2.0.0",
        "CFBundleVersion": "2.0.0",
        "NSHighResolutionCapable": True,
    },
)
EOF

echo "[8/10] Preparing macOS virtual environment..."
if [[ ! -d ".venv-mac" ]]; then
  "$PYTHON_BIN" -m venv .venv-mac
fi
source .venv-mac/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-mac.txt

python - <<'PY'
import flask
import waitress
import webview
import Cocoa
import Quartz
import WebKit
print("Flask / Waitress / pywebview / PyObjC imports: OK")
PY

echo "[9/10] Building universal ATLAS.app..."
rm -rf build dist dmg-stage ATLAS-V2.0-Universal.dmg
python -m PyInstaller --clean --noconfirm build-macos.spec

APP_BINARY="dist/ATLAS.app/Contents/MacOS/ATLAS"
if [[ ! -f "$APP_BINARY" ]]; then
  echo "ERROR: $APP_BINARY was not created."
  exit 1
fi

file "$APP_BINARY"
APP_ARCHS="$(lipo -archs "$APP_BINARY" 2>/dev/null || true)"
echo "ATLAS architectures: ${APP_ARCHS}"

if [[ "$APP_ARCHS" != *"arm64"* || "$APP_ARCHS" != *"x86_64"* ]]; then
  echo "ERROR: ATLAS.app is not universal2."
  exit 1
fi

codesign --force --deep --sign - dist/ATLAS.app
codesign --verify --deep --strict --verbose=2 dist/ATLAS.app || true

echo "[10/10] Creating ATLAS-V2.0-Universal.dmg..."
mkdir -p dmg-stage
cp -R dist/ATLAS.app dmg-stage/ATLAS.app
ln -s /Applications dmg-stage/Applications
hdiutil create \
  -volname "ATLAS V2.0" \
  -srcfolder dmg-stage \
  -ov \
  -format UDZO \
  ATLAS-V2.0-Universal.dmg

echo
echo "============================================================"
echo " BUILD COMPLETE"
echo "============================================================"
echo "App:  ${ROOT_DIR}/dist/ATLAS.app"
echo "DMG:  ${ROOT_DIR}/ATLAS-V2.0-Universal.dmg"
echo "Arch: ${APP_ARCHS}"
ls -lh ATLAS-V2.0-Universal.dmg

echo
echo "Test app with:"
echo "  open dist/ATLAS.app"
echo
echo "Optional Intel-slice test on Apple Silicon:"
echo "  softwareupdate --install-rosetta --agree-to-license"
echo "  arch -x86_64 dist/ATLAS.app/Contents/MacOS/ATLAS"
echo
echo "This is an ad-hoc signed internal/testing build."
echo "For wide distribution, use Developer ID signing + notarization."
