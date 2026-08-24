# ATLAS (SQLite + EXE-ready)

## Core fixes
- Reads profiles from `~/.aws/config` and `~/.aws/credentials` into SQLite (no secrets stored).
- Runs **SSM port-forward from CMD** with **file:// parameters JSON** (no JSON quoting issues).
- Per-user data directory (works for EXE builds).

## Run
```bat
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

REM Put catalog.json into your data dir OR set:
REM set DASHBOARD_CATALOG=C:\path\catalog.json

.venv\Scripts\python main.py
```

## Build EXE (PyInstaller)
```bat
.venv\Scripts\pip install pyinstaller
py -m PyInstaller --noconfirm --onefile ^
  --add-data "app;app" ^
  --add-data "schema.sql;." ^
  main.py
```

## Notes
- Users must have AWS CLI v2 installed and `aws` available on PATH.
- SSM Port Forward opens in a new CMD window. Close it to stop forwarding.
