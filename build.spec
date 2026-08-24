# build.spec
# PyInstaller spec to package ATLAS as Windows onedir app using pywebview
# Produces:
#   dist\ATLAS\ATLAS.exe

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# --- pywebview collections ---
webview_hidden = collect_submodules("webview")
webview_datas = collect_data_files("webview")

hidden = [
    # server/runtime
    "waitress",
    "flask",
    "sqlite3",

    # tkinter is used for friendly error dialog + browser-mode fallback
    "tkinter",
    "tkinter.messagebox",

    # pywebview common backends on Windows
    "webview",
    "webview.platforms",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.window",
] + webview_hidden

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    ("schema.sql", "."),
    ("main.py", "."),
    ("assets", "assets"),
] + webview_datas

a = Analysis(
    ["launcher_desktop.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    icon=r"assets\icon.ico",
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="ATLAS",
)