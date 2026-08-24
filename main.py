import os
import sys
import re
import json
import csv
import uuid
import shlex
import threading
import socket
import subprocess
import datetime
import sqlite3
import webbrowser
import html
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple

UTC = datetime.timezone.utc


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _utc_now_iso_full() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _utc_now_iso_seconds() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

# --------------------------------------------------------------------------------------
# App config
# --------------------------------------------------------------------------------------

APP_TITLE = "AWS Trusted Login & Access Service"
APP_VERSION = "2.0.0"
APP_DATA_FOLDER_NAME = "ATLAS"

BASE_DIR = os.path.dirname(__file__)


def get_app_data_dir(app_name: str = APP_DATA_FOLDER_NAME) -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Local")
        return Path(base) / app_name

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / app_name
    return Path.home() / ".local" / "share" / app_name


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


APP_DATA_DIR = get_app_data_dir()
LOG_DIR = APP_DATA_DIR / "logs"
EXPORT_DIR_PATH = APP_DATA_DIR / "exports"

ensure_dirs(APP_DATA_DIR, LOG_DIR, EXPORT_DIR_PATH)

DB_PATH = str(APP_DATA_DIR / "catalog.db")
CATALOG_JSON_PATH = str(APP_DATA_DIR / "catalog.json")  # legacy fallback only


def safe_catalog_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unknown")


def catalog_json_path_for(profile: str, region: str) -> str:
    profile_safe = safe_catalog_name(profile)
    region_safe = safe_catalog_name(region)
    return str(APP_DATA_DIR / f"catalog_{profile_safe}_{region_safe}.json")


def catalog_is_fresh(profile: str, region: str, max_age_hours: int = 24) -> bool:
    path = catalog_json_path_for(profile, region)
    if not os.path.exists(path):
        return False

    try:
        modified_utc = datetime.datetime.fromtimestamp(os.path.getmtime(path), UTC)
        age = _utc_now() - modified_utc
        return age.total_seconds() < (max_age_hours * 3600)
    except Exception:
        return False


def catalog_status(profile: str, region: str, max_age_hours: int = 24) -> Dict[str, Any]:
    """Display-friendly freshness information for one cached regional catalog."""
    path = catalog_json_path_for(profile, region)
    result: Dict[str, Any] = {
        "exists": False, "fresh": False, "status": "Missing",
        "age_seconds": None, "age_text": "Never",
        "last_refresh_utc": "", "path": path,
    }
    if not os.path.exists(path):
        return result
    try:
        modified_utc = datetime.datetime.fromtimestamp(os.path.getmtime(path), UTC)
        age_seconds = max(0, int((_utc_now() - modified_utc).total_seconds()))
        if age_seconds < 60:
            age_text = "just now"
        elif age_seconds < 3600:
            age_text = f"{age_seconds // 60} min ago"
        elif age_seconds < 86400:
            age_text = f"{age_seconds // 3600} hr ago"
        else:
            age_text = f"{age_seconds // 86400} day(s) ago"
        fresh = age_seconds < (max_age_hours * 3600)
        result.update({
            "exists": True, "fresh": fresh,
            "status": "Fresh" if fresh else "Stale",
            "age_seconds": age_seconds, "age_text": age_text,
            "last_refresh_utc": modified_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
    except Exception:
        result["status"] = "Unknown"
    return result
SCHEMA_SQL_PATH = os.path.join(BASE_DIR, "schema.sql")
EXPORT_DIR = str(EXPORT_DIR_PATH)

JOB_LOG_PATH = str(LOG_DIR / "jobs.log")
ACTIVE_TUNNELS_STATE_PATH = str(APP_DATA_DIR / "active_tunnels.json")

DEFAULT_REGIONS = [
    "us-east-1", "us-east-2",
    "us-west-1", "us-west-2",
    "eu-west-1", "eu-central-1",
    "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
]

# Major regions used by ATLAS for the first automatic inventory build.
# Other regions remain selectable and will build only when explicitly selected/refreshed.
PRIMARY_AUTO_BUILD_REGIONS = ["us-east-1", "eu-west-1", "ap-northeast-1"]

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")

# --------------------------------------------------------------------------------------
# Active SSM tunnel registry for idle keepalive
# --------------------------------------------------------------------------------------

ACTIVE_TUNNELS: Dict[str, Dict[str, Any]] = {}
ACTIVE_TUNNELS_LOCK = threading.Lock()
ACTIVE_TUNNEL_START_GRACE_SECONDS = 60
ACTIVE_TUNNEL_HEALTH_INTERVAL_SECONDS = 30
ACTIVE_TUNNEL_STALE_RETENTION_SECONDS = 24 * 60 * 60
ACTIVE_TUNNEL_AUTO_RECONNECT_MAX = 1
ACTIVE_TUNNEL_AUTO_RECONNECT_COOLDOWN_SECONDS = 120


def _persist_active_tunnels_locked() -> None:
    """Persist active tunnel metadata so the session widget survives app restart.

    This intentionally stores only metadata + PID + restart payload. It does not
    create new tunnels during startup. The backend health monitor will mark stale
    records and the existing Restart button can start them again safely.
    """
    try:
        rows = list(ACTIVE_TUNNELS.values())
        tmp_path = ACTIVE_TUNNELS_STATE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"saved_utc": _utc_now_iso_full(), "tunnels": rows}, f, indent=2)
        os.replace(tmp_path, ACTIVE_TUNNELS_STATE_PATH)
    except Exception:
        pass


def persist_active_tunnels() -> None:
    with ACTIVE_TUNNELS_LOCK:
        _persist_active_tunnels_locked()


def load_persisted_active_tunnels() -> None:
    """Load previous active tunnel metadata after an ATLAS restart."""
    if not os.path.exists(ACTIVE_TUNNELS_STATE_PATH):
        return
    try:
        with open(ACTIVE_TUNNELS_STATE_PATH, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        rows = data.get("tunnels") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return

        restored: Dict[str, Dict[str, Any]] = {}
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            tunnel_id = str(rec.get("id") or uuid.uuid4())
            rec["id"] = tunnel_id
            rec["status"] = "restored"
            rec["last_status"] = "restored from previous app state"
            rec["restored_utc"] = _utc_now_iso_full()
            # Important: restored rows are display/metadata only until the daemon
            # proves the old PID/port is still valid. Do not auto-reconnect these
            # immediately on app startup; otherwise simply opening ATLAS can create
            # new background tunnels without the user clicking anything.
            rec["restored_from_disk"] = True
            rec["auto_reconnect_suspended"] = True
            restored[tunnel_id] = rec

        if restored:
            with ACTIVE_TUNNELS_LOCK:
                ACTIVE_TUNNELS.update(restored)
                _persist_active_tunnels_locked()
    except Exception:
        pass


def _utc_now_iso() -> str:
    return _utc_now_iso_seconds()


def _parse_utc_iso(value: str) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _pid_is_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            code, out, err = run_cmd(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], timeout=5)
            return code == 0 and str(pid) in (out or "")
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _stop_pid(pid: int) -> Tuple[bool, str]:
    if not pid:
        return False, "No PID available"
    try:
        if os.name == "nt":
            code, out, err = run_cmd(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=10)
            return code == 0, (out or err or "").strip()
        os.kill(int(pid), 15)
        return True, "Stopped"
    except Exception as e:
        return False, str(e)


def register_active_tunnel(
    profile: str,
    region: str,
    tunnel_type: str,
    target: str,
    remote_host: str,
    remote_port: int,
    local_port: int,
    pid: int = 0,
    label: str = "",
    url: str = "",
    restart_kind: str = "",
    restart_payload: Optional[Dict[str, Any]] = None,
) -> str:
    tunnel_id = str(uuid.uuid4())
    now = _utc_now_iso()
    rec = {
        "id": tunnel_id,
        "profile": profile,
        "region": region,
        "type": tunnel_type,
        "target": target,
        "label": label or target or remote_host or tunnel_type,
        "remote_host": remote_host or "",
        "remote_port": int(remote_port or 0),
        "local_port": int(local_port or 0),
        "pid": int(pid or 0),
        "url": url or (f"http://127.0.0.1:{int(local_port or 0)}/" if int(local_port or 0) else ""),
        "created_utc": now,
        "last_keepalive_utc": "",
        "last_status": "registered",
        "status": "active",
        "duration_seconds": 0,
        "restart_kind": restart_kind or "",
        "restart_payload": restart_payload or {},
        "auto_reconnect_count": 0,
        "last_reconnect_utc": "",
    }
    with ACTIVE_TUNNELS_LOCK:
        ACTIVE_TUNNELS[tunnel_id] = rec
        _persist_active_tunnels_locked()
    return tunnel_id



def _seconds_since_iso(value: str) -> int:
    dt = _parse_utc_iso(value or "")
    if not dt:
        return 0
    return max(0, int((_utc_now() - dt).total_seconds()))


def _set_tunnel_status_fields_locked(tunnel_id: str, checked: Dict[str, Any], now: str) -> None:
    """Update health fields while preserving the original launch metadata."""
    if tunnel_id not in ACTIVE_TUNNELS:
        return

    old_status = (ACTIVE_TUNNELS[tunnel_id].get("status") or "").lower()
    new_status = (checked.get("status") or "unknown").lower()

    ACTIVE_TUNNELS[tunnel_id]["status"] = new_status
    ACTIVE_TUNNELS[tunnel_id]["duration_seconds"] = checked.get("duration_seconds", 0)
    ACTIVE_TUNNELS[tunnel_id]["alive_by_pid"] = bool(checked.get("alive_by_pid"))
    ACTIVE_TUNNELS[tunnel_id]["alive_by_port"] = bool(checked.get("alive_by_port"))
    ACTIVE_TUNNELS[tunnel_id]["last_health_check_utc"] = now
    ACTIVE_TUNNELS[tunnel_id]["last_status"] = checked.get("last_status") or new_status

    if new_status == "stale":
        if old_status != "stale" or not ACTIVE_TUNNELS[tunnel_id].get("stale_since_utc"):
            ACTIVE_TUNNELS[tunnel_id]["stale_since_utc"] = now
    else:
        ACTIVE_TUNNELS[tunnel_id]["stale_since_utc"] = ""
        # Once a restored tunnel is proven active, it can participate in normal
        # health behavior again. Until then we avoid startup auto-reconnects.
        if new_status == "active":
            ACTIVE_TUNNELS[tunnel_id]["auto_reconnect_suspended"] = False
            ACTIVE_TUNNELS[tunnel_id]["restored_from_disk"] = False


def _should_cleanup_stale_tunnel(rec: Dict[str, Any]) -> bool:
    """Remove old ghost rows from the active-session widget after a safe retention window."""
    if (rec.get("status") or "").lower() != "stale":
        return False
    stale_for = _seconds_since_iso(str(rec.get("stale_since_utc") or ""))
    return stale_for >= ACTIVE_TUNNEL_STALE_RETENTION_SECONDS

def _decorate_tunnel(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Return a health-decorated tunnel record without mutating the registry."""
    x = dict(rec)
    pid = int(x.get("pid") or 0)
    local_port = int(x.get("local_port") or 0)
    kind = (x.get("restart_kind") or "").lower().strip()
    tunnel_type = (x.get("type") or "").lower().strip()

    created = _parse_utc_iso(str(x.get("created_utc") or ""))
    if created:
        duration_seconds = max(0, int((_utc_now() - created).total_seconds()))
    else:
        duration_seconds = 0

    # Shell sessions depend only on PID. DB/Web sessions need both PID and port
    # when PID is known. If PID is absent, only the localhost port can be checked.
    if pid:
        alive_by_pid = _pid_is_running(pid)
    else:
        alive_by_pid = False if kind == "shell" or tunnel_type == "shell" else True

    alive_by_port = True
    port_message = "ok"
    if local_port > 0:
        alive_by_port, port_message = keepalive_local_port(local_port, timeout_seconds=1)

    if (kind == "shell" or tunnel_type == "shell") and not alive_by_pid:
        status = "stale"
        status_message = "shell process is not running"
    elif not alive_by_pid:
        status = "stale"
        status_message = "aws/ssm process is not running"
    elif local_port > 0 and not alive_by_port:
        status = "starting" if duration_seconds < ACTIVE_TUNNEL_START_GRACE_SECONDS else "stale"
        status_message = "waiting for local port" if status == "starting" else str(port_message)[:200]
    else:
        status = "active"
        status_message = "ok"

    x["status"] = status
    x["alive_by_pid"] = bool(alive_by_pid)
    x["alive_by_port"] = bool(alive_by_port)
    x["last_status"] = status_message
    x["duration_seconds"] = duration_seconds
    if status == "stale" and not x.get("stale_since_utc"):
        x["stale_since_utc"] = _utc_now_iso()
    return x

def list_active_tunnels(profile: str = "", region: str = "") -> List[Dict[str, Any]]:
    with ACTIVE_TUNNELS_LOCK:
        rows = [dict(x) for x in ACTIVE_TUNNELS.values()]
    if profile:
        rows = [x for x in rows if x.get("profile") == profile]
    if region:
        rows = [x for x in rows if x.get("region") == region]
    return [_decorate_tunnel(x) for x in rows]


def keepalive_local_port(local_port: int, timeout_seconds: int = 3) -> Tuple[bool, str]:
    try:
        with socket.create_connection(("127.0.0.1", int(local_port)), timeout=timeout_seconds):
            return True, "ok"
    except Exception as e:
        return False, str(e)



def _restart_tunnel_record(tunnel_id: str, reason: str = "manual") -> Tuple[bool, str, Dict[str, Any]]:
    """Restart a registered tunnel using the saved launch metadata."""
    with ACTIVE_TUNNELS_LOCK:
        old = dict(ACTIVE_TUNNELS.get(tunnel_id) or {})
    if not old:
        return False, "Tunnel not found", {}

    kind = (old.get("restart_kind") or "").lower().strip()
    payload = dict(old.get("restart_payload") or {})
    if not kind or not payload:
        return False, "This session was created before restart metadata was available. Please stop and start it again.", old

    # Stop old process first. Ignore failure because it may already be dead.
    try:
        _stop_pid(int(old.get("pid") or 0))
    except Exception:
        pass

    try:
        if kind == "db":
            pid = start_ssm_port_forward_remote_host(
                profile=payload["profile"],
                region=payload["region"],
                jumpbox_instance_id=payload["jumpbox_id"],
                remote_host=payload["remote_host"],
                remote_port=int(payload["remote_port"]),
                local_port=int(payload["local_port"]),
            )
        elif kind == "webui":
            pid = start_ssm_port_forward_ec2(
                profile=payload["profile"],
                region=payload["region"],
                instance_id=payload["instance_id"],
                remote_port=int(payload["remote_port"]),
                local_port=int(payload["local_port"]),
                auto_open=bool(payload.get("auto_open", False)),
                open_path=payload.get("open_path") or "/",
            )
        elif kind == "shell":
            pid = start_ssm_shell(
                profile=payload["profile"],
                region=payload["region"],
                instance_id=payload["instance_id"],
            )
        else:
            return False, f"Unsupported restart kind: {kind}", old

        now = _utc_now_iso()
        new_rec = dict(old)
        new_rec.update({
            "pid": int(pid or 0),
            "created_utc": now,
            "last_keepalive_utc": "",
            "last_health_check_utc": now,
            "last_reconnect_utc": now,
            "last_status": "restarted" if reason == "manual" else "auto-reconnected",
            "status": "starting",
            "duration_seconds": 0,
        })
        if reason != "manual":
            new_rec["auto_reconnect_count"] = int(old.get("auto_reconnect_count") or 0) + 1

        with ACTIVE_TUNNELS_LOCK:
            ACTIVE_TUNNELS[tunnel_id] = new_rec
            _persist_active_tunnels_locked()
        return True, "Tunnel restarted", new_rec
    except Exception as e:
        with ACTIVE_TUNNELS_LOCK:
            if tunnel_id in ACTIVE_TUNNELS:
                ACTIVE_TUNNELS[tunnel_id]["status"] = "stale"
                ACTIVE_TUNNELS[tunnel_id]["last_status"] = f"restart failed: {str(e)[:180]}"
                _persist_active_tunnels_locked()
        return False, str(e), old


def _maybe_auto_reconnect_tunnel(tunnel_id: str, checked: Dict[str, Any]) -> None:
    """Auto reconnect DB/Web tunnels once when backend health marks them stale.

    Safety rule: restored-from-disk sessions do not auto-reconnect at startup.
    They stay visible as stale so the user can restart them deliberately.
    """
    if (checked.get("status") or "").lower() != "stale":
        return

    with ACTIVE_TUNNELS_LOCK:
        current = dict(ACTIVE_TUNNELS.get(tunnel_id) or {})

    if current.get("auto_reconnect_suspended"):
        return

    kind = (checked.get("restart_kind") or current.get("restart_kind") or "").lower()
    if kind not in ("db", "webui"):
        return

    if int(current.get("auto_reconnect_count") or 0) >= ACTIVE_TUNNEL_AUTO_RECONNECT_MAX:
        return

    if int(checked.get("duration_seconds") or 0) < ACTIVE_TUNNEL_START_GRACE_SECONDS:
        return

    last_reconnect = str(current.get("last_reconnect_utc") or "")
    if last_reconnect and _seconds_since_iso(last_reconnect) < ACTIVE_TUNNEL_AUTO_RECONNECT_COOLDOWN_SECONDS:
        return

    _restart_tunnel_record(tunnel_id, reason="auto")

def _run_tunnel_health_check_once() -> None:
    """Backend health check for active SSM shells/tunnels.

    Responsibilities:
      1. Verify PID and localhost port from the backend.
      2. Mark sessions active/starting/stale without relying on browser polling.
      3. Avoid startup auto-reconnect for restored sessions.
      4. Remove old stale ghost rows after a long retention window.
      5. Persist every health-state transition for restart continuity.
    """
    with ACTIVE_TUNNELS_LOCK:
        ids = list(ACTIVE_TUNNELS.keys())

    now = _utc_now_iso()
    for tunnel_id in ids:
        with ACTIVE_TUNNELS_LOCK:
            rec = dict(ACTIVE_TUNNELS.get(tunnel_id) or {})
        if not rec:
            continue

        checked = _decorate_tunnel(rec)
        should_cleanup = False

        with ACTIVE_TUNNELS_LOCK:
            if tunnel_id in ACTIVE_TUNNELS:
                _set_tunnel_status_fields_locked(tunnel_id, checked, now)
                should_cleanup = _should_cleanup_stale_tunnel(ACTIVE_TUNNELS[tunnel_id])
                if should_cleanup:
                    ACTIVE_TUNNELS.pop(tunnel_id, None)
                _persist_active_tunnels_locked()

        if should_cleanup:
            continue

        _maybe_auto_reconnect_tunnel(tunnel_id, checked)

def _tunnel_health_monitor_loop() -> None:
    while True:
        try:
            _run_tunnel_health_check_once()
        except Exception:
            pass
        threading.Event().wait(ACTIVE_TUNNEL_HEALTH_INTERVAL_SECONDS)


def start_tunnel_health_monitor() -> None:
    if getattr(start_tunnel_health_monitor, "_started", False):
        return
    start_tunnel_health_monitor._started = True
    threading.Thread(target=_tunnel_health_monitor_loop, daemon=True, name="atlas-tunnel-health-monitor").start()


load_persisted_active_tunnels()
start_tunnel_health_monitor()


# --------------------------------------------------------------------------------------
# Helpers: AWS profile parsing
# --------------------------------------------------------------------------------------

def _read_ini(path: str) -> Dict[str, Dict[str, str]]:
    data: Dict[str, Dict[str, str]] = {}
    if not os.path.exists(path):
        return data

    current = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1].strip()
                data[current] = {}
                continue
            if "=" in line and current:
                k, v = line.split("=", 1)
                data[current][k.strip()] = v.strip()
    return data


def _aws_paths() -> Tuple[str, str]:
    home = os.path.expanduser("~")
    return (os.path.join(home, ".aws", "config"), os.path.join(home, ".aws", "credentials"))


@dataclass
class AwsProfile:
    name: str
    region: str
    environment: str
    auth_type: str
    is_sso: bool
    has_credentials: bool
    sso_start_url: str = ""
    sso_region: str = ""
    role_name: str = ""
    account_id: str = ""
    sso_session: str = ""


def load_profiles() -> List[AwsProfile]:
    config_path, cred_path = _aws_paths()
    cfg_raw = _read_ini(config_path)
    cred_raw = _read_ini(cred_path)

    def normalize_section_to_profile(section: str) -> str:
        s = (section or "").strip()
        if s.lower() == "default":
            return "default"
        if s.lower().startswith("profile "):
            return s[8:].strip()
        return s

    cfg_profiles: Dict[str, Dict[str, str]] = {}
    sso_sessions: Dict[str, Dict[str, str]] = {}

    for section, kv in cfg_raw.items():
        if section.strip().lower().startswith("sso-session "):
            sess_name = section.strip()[len("sso-session "):].strip()
            sso_sessions[sess_name] = kv
            continue
        pname = normalize_section_to_profile(section)
        cfg_profiles[pname] = kv

    cred_profiles: Dict[str, Dict[str, str]] = {k.strip(): v for k, v in cred_raw.items()}
    all_names = sorted(set(cfg_profiles.keys()) | set(cred_profiles.keys()))

    profiles: List[AwsProfile] = []
    for name in all_names:
        ck_cfg = cfg_profiles.get(name, {})
        ck_cred = cred_profiles.get(name, {})

        merged = dict(ck_cred)
        merged.update(ck_cfg)

        region = (merged.get("region") or merged.get("sso_region") or "us-east-1").strip()

        env = "unknown"
        m = re.search(r"\b(dev|qa|stage|stg|preprod|prod|perf|vnv|demo)\b", name, re.IGNORECASE)
        if m:
            raw = m.group(1).lower()
            if raw == "stg":
                raw = "stage"
            if raw == "demo":
                raw = "stage"
            env = raw

        sso_session = (merged.get("sso_session") or "").strip()
        sess_kv = sso_sessions.get(sso_session, {}) if sso_session else {}

        has_any_sso_key = any(k in merged for k in [
            "sso_start_url", "sso_region", "sso_account_id", "sso_role_name", "sso_session"
        ])

        sso_start_url = (merged.get("sso_start_url") or sess_kv.get("sso_start_url") or "").strip()
        sso_region = (merged.get("sso_region") or sess_kv.get("sso_region") or "").strip()

        is_sso = bool(has_any_sso_key or (sso_session and sso_session in sso_sessions))
        has_creds = name in cred_profiles
        auth_type = "SSO" if is_sso else ("Creds" if has_creds else "Chain/Unknown")

        profiles.append(AwsProfile(
            name=name,
            region=region,
            environment=env,
            auth_type=auth_type,
            is_sso=is_sso,
            has_credentials=has_creds,
            sso_start_url=sso_start_url,
            sso_region=sso_region,
            role_name=(merged.get("sso_role_name") or "").strip(),
            account_id=(merged.get("sso_account_id") or "").strip(),
            sso_session=sso_session,
        ))

    return profiles


# --------------------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------------------

def db_conn() -> sqlite3.Connection:
    ensure_dirs(APP_DATA_DIR, LOG_DIR, EXPORT_DIR_PATH)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    if os.path.exists(SCHEMA_SQL_PATH):
        with open(SCHEMA_SQL_PATH, "r", encoding="utf-8") as f:
            schema = f.read()
        con = db_conn()
        try:
            con.executescript(schema)
            con.commit()
        finally:
            con.close()


def ensure_tables_exist():
    con = db_conn()
    try:
        con.execute("PRAGMA foreign_keys = ON;")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS Profile (
                ProfileName     TEXT PRIMARY KEY,
                AuthType        TEXT NOT NULL DEFAULT 'none',
                SsoStartUrl     TEXT NOT NULL DEFAULT '',
                SsoRegion       TEXT NOT NULL DEFAULT '',
                AccountId       TEXT NOT NULL DEFAULT '',
                RoleName        TEXT NOT NULL DEFAULT '',
                DefaultRegion   TEXT NOT NULL DEFAULT 'us-east-1',
                OutputFormat    TEXT NOT NULL DEFAULT 'json',
                HasCredentials  INTEGER NOT NULL DEFAULT 0,
                IsEnabled       INTEGER NOT NULL DEFAULT 1,
                CreatedAtUtc    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS Target (
                TargetId         TEXT PRIMARY KEY,
                ProfileName      TEXT NOT NULL,
                DisplayName      TEXT NOT NULL,
                TargetType       TEXT NOT NULL,
                AwsTarget        TEXT NOT NULL,
                RemoteHost       TEXT NULL,
                RemotePort       INTEGER NOT NULL,
                LocalPort        INTEGER NOT NULL,
                Env              TEXT NULL,
                Region           TEXT NULL,
                GroupTitle       TEXT NULL,
                Description      TEXT NULL,
                IsEnabled        INTEGER NOT NULL DEFAULT 1,
                SortOrder        INTEGER NOT NULL DEFAULT 0,
                CreatedAtUtc     TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(ProfileName) REFERENCES Profile(ProfileName) ON DELETE CASCADE
            )
            """
        )

        con.execute("CREATE INDEX IF NOT EXISTS IX_Target_ProfileName ON Target(ProfileName)")
        con.execute("CREATE INDEX IF NOT EXISTS IX_Target_Env ON Target(Env)")
        con.execute("CREATE INDEX IF NOT EXISTS IX_Target_GroupTitle ON Target(GroupTitle)")

        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS UX_Target_Natural
            ON Target(
              ProfileName,
              AwsTarget,
              IFNULL(RemoteHost,''),
              RemotePort,
              IFNULL(Region,''),
              IFNULL(GroupTitle,'')
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS UpdateJob (
                JobId TEXT PRIMARY KEY,
                ProfileName TEXT NOT NULL,
                Region TEXT NOT NULL,
                Status TEXT NOT NULL,
                Pid INTEGER NOT NULL DEFAULT 0,
                CreatedAtUtc TEXT NOT NULL,
                LastUpdateUtc TEXT NOT NULL,
                LogText TEXT NOT NULL DEFAULT ''
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS JumpboxPreference (
                ProfileName TEXT NOT NULL,
                Region      TEXT NOT NULL,
                Env         TEXT NOT NULL,
                TargetType  TEXT NOT NULL,
                JumpboxId   TEXT NOT NULL,
                UpdatedAtUtc TEXT NOT NULL,
                PRIMARY KEY (ProfileName, Region, Env, TargetType)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS FavoriteTarget (
                ProfileName   TEXT NOT NULL,
                Region        TEXT NOT NULL,
                TargetId      TEXT NOT NULL,
                CreatedAtUtc  TEXT NOT NULL,
                PRIMARY KEY (ProfileName, Region, TargetId)
            )
            """
        )

        con.commit()
    finally:
        con.close()


init_db()
ensure_tables_exist()


# --------------------------------------------------------------------------------------
# Jumpbox Preference (DB)
# --------------------------------------------------------------------------------------

def get_jumpbox_preference(profile: str, region: str, env: str, target_type: str) -> Optional[str]:
    ensure_tables_exist()
    con = db_conn()
    try:
        row = con.execute(
            """
            SELECT JumpboxId FROM JumpboxPreference
            WHERE ProfileName=? AND Region=? AND Env=? AND TargetType=?
            """,
            (profile, region, (env or "OTHER").upper(), (target_type or "db").lower()),
        ).fetchone()
        return row["JumpboxId"] if row else None
    finally:
        con.close()


def set_jumpbox_preference(profile: str, region: str, env: str, target_type: str, jumpbox_id: str) -> None:
    ensure_tables_exist()
    con = db_conn()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO JumpboxPreference(ProfileName, Region, Env, TargetType, JumpboxId, UpdatedAtUtc)
            VALUES(?,?,?,?,?,?)
            """,
            (
                profile,
                region,
                (env or "OTHER").upper(),
                (target_type or "db").lower(),
                jumpbox_id,
                _utc_now_iso_full(),
            ),
        )
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

def _append_job_file_log(job_id: str, text: str) -> None:
    try:
        with open(JOB_LOG_PATH, "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"\n[{_utc_now_iso_full()}] job={job_id}\n")
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# AWS CLI discovery
# --------------------------------------------------------------------------------------

def find_aws_exe() -> Optional[str]:
    import shutil

    override = (os.environ.get("AWS_CLI_PATH") or "").strip().strip('"')
    if override and os.path.isfile(override):
        return override

    p = shutil.which("aws")
    if p and os.path.isfile(p):
        return p

    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Amazon\AWSCLIV2\aws.exe",
            r"C:\Program Files (x86)\Amazon\AWSCLIV2\aws.exe",
        ]
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            candidates.append(os.path.join(la, "Amazon", "AWSCLIV2", "aws.exe"))
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    candidates = [
        "/opt/homebrew/bin/aws",
        "/usr/local/bin/aws",
        "/usr/bin/aws",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def aws_cmd_base() -> List[str]:
    aws = find_aws_exe()
    if not aws:
        raise RuntimeError(
            "AWS CLI not found.\n\n"
            "Please install AWS CLI v2.\n"
            "Or set AWS_CLI_PATH to full path of aws.\n"
        )
    return [aws]


# --------------------------------------------------------------------------------------
# AWS helpers
# --------------------------------------------------------------------------------------

def run_cmd(cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 1800) -> Tuple[int, str, str]:
    try:
        creationflags = 0
        startupinfo = None

        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env or os.environ.copy(),
            shell=False,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    except FileNotFoundError as e:
        return 127, "", (
            f"Command not found: {cmd[0]}\n{e}\n\n"
            "Fix: Install AWS CLI v2.\n"
            "Tip: If aws is installed but PATH is not updated, set AWS_CLI_PATH.\n"
        )
    except Exception as e:
        return 128, "", f"Failed to start command: {' '.join(cmd)}\n{e}"

    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return 124, out, (err + "\nTIMEOUT")

    return p.returncode, out, err


class SsoSessionExpiredError(RuntimeError):
    pass


def _aws_error_text(out: str = "", err: str = "") -> str:
    return (err or out or "").strip()


def is_sso_session_expired_text(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "the sso session associated with this profile has expired",
        "to refresh this sso session run aws sso login",
        "sso session has expired",
        "session token found in the aws_sso cache is expired",
        "token has expired and refresh failed",
        "unauthorizedexception",
        "invalid_grant",
        "expiredtoken",
        "expired token",
    ]
    return any(x in t for x in patterns)


def sso_relogin_url(profile: str) -> str:
    return url_for("sso_login", profile=profile)


def check_aws_profile_auth(profile: str, region: str = "us-east-1") -> Dict[str, Any]:
    """
    Fast preflight auth check. Used before catalog refresh/build so users get a clear
    SSO re-login message instead of a long AWS CLI failure.
    """
    try:
        cmd = aws_cmd_base() + [
            "sts", "get-caller-identity",
            "--profile", profile,
            "--region", region or "us-east-1",
            "--output", "json",
        ]
        code, out, err = run_cmd(cmd, timeout=30)
        msg = _aws_error_text(out, err)

        if code == 0:
            ident = {}
            try:
                ident = json.loads(out or "{}")
            except Exception:
                ident = {}
            return {"ok": True, "expired": False, "identity": ident, "message": "ok"}

        expired = is_sso_session_expired_text(msg)
        return {"ok": False, "expired": expired, "identity": {}, "message": msg or f"AWS CLI returned exit code {code}"}
    except Exception as e:
        return {"ok": False, "expired": False, "identity": {}, "message": str(e)}


def aws_cli_json(profile: str, region: str, service_args: List[str], log_cb) -> Any:
    cmd = aws_cmd_base() + service_args + ["--profile", profile, "--region", region, "--output", "json"]
    log_cb(f"$ {' '.join(shlex.quote(x) for x in cmd)}\n")
    code, out, err = run_cmd(cmd)
    if code != 0:
        msg = _aws_error_text(out, err)
        if is_sso_session_expired_text(msg):
            raise SsoSessionExpiredError(
                "SSO session expired for profile: " + profile +
                "\n\nGo to Profiles, click SSO Login for this profile, and retry the action.\n\n" + msg
            )
        raise RuntimeError(f"AWS CLI failed ({code})\n{msg}")
    try:
        return json.loads(out)
    except Exception as e:
        raise RuntimeError(f"Failed to parse AWS JSON: {e}\nRaw:\n{out[:2000]}")


def classify_ec2_platform(inst: Dict[str, Any]) -> str:
    plat = (inst.get("Platform") or "").lower()
    details = (inst.get("PlatformDetails") or "").lower()
    if "windows" in plat or "windows" in details:
        return "windows"
    return "linux"


def map_db_tech(engine: str) -> str:
    e = (engine or "").lower()
    if "sqlserver" in e:
        return "MSSQL"
    if "postgres" in e:
        return "PostgreSQL"
    if "mysql" in e or "mariadb" in e:
        return "MySQL"
    if "aurora" in e:
        return "Aurora"
    if "docdb" in e:
        return "DocDB"
    return "Other DB"


def map_cluster_group(engine: str) -> str:
    e = (engine or "").lower()
    if "docdb" in e:
        return "DocDB Cluster"
    if "postgres" in e:
        return "PostgreSQL Cluster"
    if "mysql" in e or "mariadb" in e:
        return "MySQL Cluster"
    if "aurora" in e:
        return "Aurora Cluster"
    return "Cluster"


def stable_local_port(seed: str, base: int = 30000, span: int = 20000) -> int:
    h = 0
    for ch in seed:
        h = (h * 131 + ord(ch)) & 0x7FFFFFFF
    return base + (h % span)


# ✅ UPDATED: better env detection for names like covnv/coperf/costg/coprd/codev
def infer_env(name: str) -> str:
    n = (name or "").lower().strip()
    if not n:
        return "OTHER"

    n = n.replace("-stg", "-stage").replace("_stg", "_stage")

    if "vnv" in n:
        return "VNV"
    if "perf" in n:
        return "PERF"
    if "preprod" in n:
        return "PREPROD"
    if "stage" in n or "stg" in n or "demo" in n:
        return "STAGE"
    if "qa" in n:
        return "QA"
    if "dev" in n:
        return "DEV"
    if "prod" in n or "prd" in n:
        return "PROD"

    return "OTHER"



def _dns_norm(value: str) -> str:
    return (value or "").strip().lower().rstrip(".")


def _route53_values(rr: Dict[str, Any]) -> List[str]:
    vals = []
    for x in rr.get("ResourceRecords", []) or []:
        v = _dns_norm(x.get("Value") or "")
        if v:
            vals.append(v)
    alias = rr.get("AliasTarget") or {}
    alias_dns = _dns_norm(alias.get("DNSName") or "")
    if alias_dns:
        vals.append(alias_dns)
    return vals


def discover_route53_records(profile: str, region: str, log_cb) -> List[Dict[str, Any]]:
    """
    Live Route53 discovery is intentionally disabled.

    Enterprise mode: ATLAS does not require every user to have Route53/ELB access.
    DNS/CNAME/Alias values can be supplied later from an external mapping file:
      - dns_mapping.json
      - dns_mapping.csv

    The dashboard still keeps the DNS/Alias field on cards, so wiring this later
    does not need another UI change.
    """
    try:
        log_cb("Route53 live discovery disabled. Using external DNS mapping file if present.\n")
    except Exception:
        pass
    return []

def _route53_record_key(record_name: str) -> str:
    return _dns_norm(record_name)


def _route53_target_values(rr: Dict[str, Any]) -> List[str]:
    return [_dns_norm(v) for v in (rr.get("values") or []) if _dns_norm(v)]


def _endpoint_variants(value: str) -> List[str]:
    """Return normalized variants for matching Route53 targets to AWS endpoints/IPs."""
    v = _dns_norm(value)
    if not v:
        return []
    variants = {v}
    # Route53 alias targets often add trailing dots; _dns_norm already handles that.
    # Add shortened variants to handle AWS endpoint aliases and regional suffix differences.
    shortened = v
    for suffix in (
        ".rds.amazonaws.com",
        ".docdb.amazonaws.com",
        ".cache.amazonaws.com",
        ".elb.amazonaws.com",
        ".amazonaws.com",
    ):
        shortened = shortened.replace(suffix, "")
    if shortened and shortened != v:
        variants.add(shortened)
    return [x for x in variants if x]


def match_route53_records(records: List[Dict[str, Any]], *targets: str) -> List[str]:
    """
    Return friendly Route53 record names that point to any supplied AWS target.

    Supports:
      - A/AAAA records -> private/public IP
      - CNAME records -> RDS/DocDB/EC2 private DNS/public DNS/AWS endpoint
      - Alias records -> ELB/ALB or AWS DNS targets
      - chained CNAMEs, e.g. app.company.com -> app-int.company.com -> AWS endpoint
      - partial AWS endpoint matching for generated endpoint suffix differences
    """
    raw_needles = [_dns_norm(t) for t in targets if _dns_norm(t)]
    if not raw_needles:
        return []

    # record name -> record and value lookup
    name_to_rr: Dict[str, Dict[str, Any]] = {}
    value_to_names: Dict[str, List[str]] = {}
    all_records = records or []

    for rr in all_records:
        rr_name = _route53_record_key(rr.get("name") or "")
        if not rr_name:
            continue
        name_to_rr[rr_name] = rr
        for val in _route53_target_values(rr):
            value_to_names.setdefault(val, []).append(rr_name)

    needles = set()
    for n in raw_needles:
        needles.update(_endpoint_variants(n))

    matched_names: set[str] = set()

    def value_matches_needles(value: str) -> bool:
        v = _dns_norm(value)
        if not v:
            return False
        variants = set(_endpoint_variants(v))
        for n in needles:
            if not n:
                continue
            if n in variants or v == n:
                return True
            # Partial match for AWS generated names and alias targets.
            for vv in variants:
                if vv and n and (vv in n or n in vv):
                    # Avoid unsafe 1-2 character matches.
                    if len(vv) >= 8 and len(n) >= 8:
                        return True
        return False

    # First pass: records directly pointing to target values.
    for rr in all_records:
        rr_name = _route53_record_key(rr.get("name") or "")
        if not rr_name:
            continue
        values = _route53_target_values(rr)
        if any(value_matches_needles(v) for v in values) or value_matches_needles(rr_name):
            matched_names.add(rr_name)

    # Chained CNAME/Alias pass: if a record points to an already matched record name,
    # include that parent record too. Repeat a few times for multi-hop chains.
    changed = True
    hops = 0
    while changed and hops < 8:
        changed = False
        hops += 1
        for rr in all_records:
            rr_name = _route53_record_key(rr.get("name") or "")
            if not rr_name or rr_name in matched_names:
                continue
            values = set(_route53_target_values(rr))
            if values & matched_names:
                matched_names.add(rr_name)
                changed = True

    return sorted(matched_names)



def _is_elb_dns(value: str) -> bool:
    v = _dns_norm(value)
    return ".elb.amazonaws.com" in v or ".elb." in v


def _route53_names_for_elb_dns(records: List[Dict[str, Any]], elb_dns: str) -> List[str]:
    """Return Route53 record names that point to an ELB DNS name, including parent CNAME/Alias chains."""
    return match_route53_records(records, elb_dns)


def discover_elb_target_dns_mapping(
    profile: str,
    region: str,
    route53_records: List[Dict[str, Any]],
    ec2_targets: List[Dict[str, Any]],
    log_cb,
) -> Dict[str, List[str]]:
    """
    Live ELB/target-group correlation is intentionally disabled.

    Reason: this requires elasticloadbalancing:* describe permissions, which may not
    be available for all users. Use external DNS mapping instead.
    """
    try:
        log_cb("ELB live DNS correlation disabled. External DNS mapping can populate card aliases.\n")
    except Exception:
        pass
    return {}

def _desc_join(parts: Dict[str, Any]) -> str:
    safe = []
    for k, v in parts.items():
        if v is None:
            v = ""
        if k == "members_json":
            v = json.dumps(v or [], separators=(",", ":"))
        elif isinstance(v, (list, tuple)):
            v = "|".join(str(x) for x in v if str(x))
        safe.append(f"{k}={str(v).replace(';', ',')}")
    return "; ".join(safe)


def _split_dns_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[|,;\n\r]+", str(value))
    out = []
    for x in raw:
        v = str(x or "").strip().strip(".")
        if v and v.lower() not in ("not mapped", "none", "null", "-") and v not in out:
            out.append(v)
    return out


def load_external_dns_mapping(log_cb=None) -> List[Dict[str, Any]]:
    r"""
    Load optional DNS/CNAME/Alias mapping without requiring Route53 permissions.

    Supported locations:
      1) <ATLAS app data>\dns_mapping.json
      2) <ATLAS app data>\dns_mapping.csv
      3) <ATLAS app folder>\dns_mapping.json
      4) <ATLAS app folder>\dns_mapping.csv

    JSON accepted shapes:
      [ { ...mapping... }, ... ]
      { "mappings": [ { ...mapping... } ] }
      { "records": [ { ...mapping... } ] }

    Useful columns/keys:
      profile, region, target_id, instance_id, db_id, cluster_id, name,
      endpoint, private_ip, aws_target, dns_names, dns, cname, alias
    """
    candidates = [
        APP_DATA_DIR / "dns_mapping.json",
        APP_DATA_DIR / "dns_mapping.csv",
        Path(BASE_DIR) / "dns_mapping.json",
        Path(BASE_DIR) / "dns_mapping.csv",
    ]
    rows: List[Dict[str, Any]] = []

    for path in candidates:
        if not path.exists():
            continue
        try:
            if path.suffix.lower() == ".json":
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    data = data.get("mappings") or data.get("records") or data.get("items") or []
                if isinstance(data, list):
                    rows.extend([x for x in data if isinstance(x, dict)])
            elif path.suffix.lower() == ".csv":
                with path.open("r", encoding="utf-8-sig", newline="") as f:
                    rows.extend(list(csv.DictReader(f)))
            if log_cb:
                log_cb(f"External DNS mapping loaded from {path}: {len(rows)} total row(s).\n")
        except Exception as e:
            if log_cb:
                log_cb(f"External DNS mapping skipped for {path}: {e}\n")

    return rows


def apply_external_dns_mappings(inv: Dict[str, Any], log_cb=None) -> None:
    mappings = load_external_dns_mapping(log_cb)
    if not mappings:
        if log_cb:
            log_cb("External DNS mapping file not found. DNS/Alias fields will show as Not mapped.\n")
        return

    profile = _dns_norm(inv.get("profile") or "")
    region = _dns_norm(inv.get("region") or "")

    def values_for_row(row: Dict[str, Any]) -> List[str]:
        vals: List[str] = []
        for key in ("dns_names", "dns", "cname", "alias", "aliases", "route53_records"):
            for v in _split_dns_values(row.get(key)):
                if v not in vals:
                    vals.append(v)
        return vals

    def row_applies(row: Dict[str, Any]) -> bool:
        rp = _dns_norm(row.get("profile") or "")
        rr = _dns_norm(row.get("region") or "")
        if rp and rp != profile:
            return False
        if rr and rr not in (region, "all", "*"):
            return False
        return True

    def target_keys(item: Dict[str, Any]) -> set[str]:
        keys = set()
        for key in ("target_id", "instance_id", "db_id", "cluster_id", "name", "endpoint", "private_ip", "private_dns", "public_dns", "aws_target"):
            v = _dns_norm(item.get(key) or "")
            if v:
                keys.add(v)
        return keys

    def row_keys(row: Dict[str, Any]) -> set[str]:
        keys = set()
        for key in ("target_id", "instance_id", "db_id", "cluster_id", "name", "endpoint", "private_ip", "private_dns", "public_dns", "aws_target", "aws_id"):
            v = _dns_norm(row.get(key) or "")
            if v:
                keys.add(v)
        return keys

    applied = 0

    def apply_to_item(item: Dict[str, Any]) -> None:
        nonlocal applied
        ikeys = target_keys(item)
        if not ikeys:
            return
        existing = list(item.get("route53_records") or [])
        changed = False
        for row in mappings:
            if not row_applies(row):
                continue
            rkeys = row_keys(row)
            if not rkeys or not (ikeys & rkeys):
                continue
            for dns in values_for_row(row):
                if dns not in existing:
                    existing.append(dns)
                    changed = True
        if changed:
            item["route53_records"] = existing
            item["dns_mapping_source"] = "external_mapping"
            applied += 1

    for collection in ("ec2", "rds_instances", "rds_clusters", "docdb_clusters"):
        for item in inv.get(collection, []) or []:
            apply_to_item(item)
            for member in item.get("members", []) or []:
                if isinstance(member, dict):
                    apply_to_item(member)

    if log_cb:
        log_cb(f"External DNS mappings applied to {applied} card(s).\n")

def build_inventory(profile: str, region: str, log_cb) -> Dict[str, Any]:
    inv: Dict[str, Any] = {
        "profile": profile,
        "region": region,
        "generated_utc": _utc_now_iso_full(),
        "ec2": [],
        "rds_instances": [],
        "rds_clusters": [],
        "docdb_clusters": [],
        "route53_records": [],
    }

    route53_records = discover_route53_records(profile, region, log_cb)
    inv["route53_records"] = route53_records
    log_cb(f"Loaded Route53 records: {len(route53_records)}\n")

    for rr in route53_records[:20]:
        try:
            log_cb(
                f"Route53: {rr.get('name')} -> "
                f"{','.join(rr.get('values') or [])}\n"
            )
        except Exception:
            pass

    ec2 = aws_cli_json(profile, region, ["ec2", "describe-instances"], log_cb)
    seen_ec2 = set()
    for res in ec2.get("Reservations", []):
        for inst in res.get("Instances", []):
            iid = inst.get("InstanceId")
            if not iid or iid in seen_ec2:
                continue
            seen_ec2.add(iid)

            name = ""
            for t in inst.get("Tags", []) or []:
                if t.get("Key") == "Name":
                    name = t.get("Value") or ""
                    break

            state = (inst.get("State") or {}).get("Name", "unknown")
            platform = classify_ec2_platform(inst)

            remote_port = 3389 if platform == "windows" else 22
            local_port = stable_local_port(f"ec2|{profile}|{region}|{iid}|{remote_port}")
            private_ip = inst.get("PrivateIpAddress") or ""
            private_dns = inst.get("PrivateDnsName") or ""
            public_ip = inst.get("PublicIpAddress") or ""
            public_dns = inst.get("PublicDnsName") or ""
            r53 = match_route53_records(route53_records, private_ip, private_dns, public_ip, public_dns)

            inv["ec2"].append(
                {
                    "instance_id": iid,
                    "name": name or iid,
                    "state": state,
                    "platform": platform,
                    "remote_port": remote_port,
                    "local_port": local_port,
                    "private_ip": private_ip,
                    "private_dns": private_dns,
                    "public_ip": public_ip,
                    "public_dns": public_dns,
                    "route53_records": r53,
                    "target_id": iid,
                }
            )


    # Route53 -> ELB/ALB/NLB -> TargetGroup -> EC2 correlation.
    # This attaches friendly service DNS names (A/CNAME/Alias records that point to ELBs)
    # to the backend EC2 cards, so users can search by the DNS they actually know.
    try:
        elb_dns_by_iid = discover_elb_target_dns_mapping(profile, region, route53_records, inv.get("ec2", []), log_cb)
        if elb_dns_by_iid:
            for inst in inv.get("ec2", []):
                iid = inst.get("instance_id") or ""
                extra = elb_dns_by_iid.get(iid) or []
                if extra:
                    merged = []
                    for n in list(inst.get("route53_records") or []) + extra:
                        nn = _dns_norm(n)
                        if nn and nn not in merged:
                            merged.append(nn)
                    inst["route53_records"] = sorted(merged)
            log_cb(f"Route53 ELB target mappings applied to EC2 instances: {len(elb_dns_by_iid)}\n")
        else:
            log_cb("Route53 ELB target mappings applied to EC2 instances: 0\n")
    except Exception as e:
        log_cb(f"Route53 ELB target mapping skipped: {e}\n")

    clu = aws_cli_json(profile, region, ["rds", "describe-db-clusters"], log_cb)
    seen_clu = set()
    for c in clu.get("DBClusters", []):
        cid = c.get("DBClusterIdentifier")
        if not cid or cid in seen_clu:
            continue
        seen_clu.add(cid)

        engine = c.get("Engine", "")
        endpoint = c.get("Endpoint") or ""
        port = c.get("Port") or 0
        status = c.get("Status", "") or "unknown"
        members = []
        for mem in c.get("DBClusterMembers", []) or []:
            mid = mem.get("DBInstanceIdentifier")
            if mid:
                members.append(mid)

        local_port = stable_local_port(f"rds_cluster|{profile}|{region}|{cid}|{port}")
        group = map_cluster_group(engine)
        r53 = match_route53_records(route53_records, endpoint)

        inv["rds_clusters"].append(
            {
                "cluster_id": cid,
                "name": cid,
                "engine": engine,
                "tech": map_db_tech(engine),
                "cluster_group": group,
                "endpoint": endpoint,
                "port": int(port) if port else 0,
                "status": status,
                "local_port": local_port,
                "member_names": members,
                "members": [],
                "route53_records": r53,
                "target_id": cid,
            }
        )

    rds = aws_cli_json(profile, region, ["rds", "describe-db-instances"], log_cb)
    seen_rds = set()
    for db in rds.get("DBInstances", []):
        dbid = db.get("DBInstanceIdentifier")
        if not dbid or dbid in seen_rds:
            continue
        seen_rds.add(dbid)

        cluster_id = db.get("DBClusterIdentifier") or ""
        engine = db.get("Engine", "")
        tech = map_db_tech(engine)
        endpoint = (db.get("Endpoint") or {}).get("Address", "")
        port = (db.get("Endpoint") or {}).get("Port", 0) or 0
        status = db.get("DBInstanceStatus", "") or "unknown"
        local_port = stable_local_port(f"rds|{profile}|{region}|{dbid}|{port}")
        r53 = match_route53_records(route53_records, endpoint)

        if cluster_id:
            member = {
                "name": dbid,
                "endpoint": endpoint,
                "remote_port": int(port) if port else 0,
                "local_port": local_port,
                "status": status,
                "engine": engine,
                "aws_target": dbid,
                "target_type": "rds_cluster_instance",
                "route53_records": r53,
                "can_forward": status.lower() in ("available", "running"),
            }
            for c in inv["rds_clusters"]:
                if c.get("cluster_id") == cluster_id:
                    c.setdefault("members", []).append(member)
                    break
            continue

        inv["rds_instances"].append(
            {
                "db_id": dbid,
                "name": dbid,
                "engine": engine,
                "tech": tech,
                "endpoint": endpoint,
                "port": int(port) if port else 0,
                "status": status,
                "local_port": local_port,
                "route53_records": r53,
                "target_id": dbid,
            }
        )

    # DocDB
    try:
        doc = aws_cli_json(profile, region, ["docdb", "describe-db-clusters"], log_cb)
        seen_doc = set()
        doc_map: Dict[str, Dict[str, Any]] = {}

        for c in doc.get("DBClusters", []):
            cid = c.get("DBClusterIdentifier")
            if not cid or cid in seen_doc:
                continue
            seen_doc.add(cid)

            endpoint = (c.get("Endpoint") or "").strip()
            port = int(c.get("Port") or 27017)
            status = c.get("Status", "") or "unknown"

            if port != 27017:
                continue
            if endpoint and ("docdb.amazonaws.com" not in endpoint):
                continue

            local_port = stable_local_port(f"docdb_cluster|{profile}|{region}|{cid}|{port}")
            r53 = match_route53_records(route53_records, endpoint)

            rec = {
                "cluster_id": cid,
                "name": cid,
                "engine": "docdb",
                "tech": "DocDB",
                "cluster_group": "DocDB Cluster",
                "endpoint": endpoint,
                "port": 27017,
                "status": status,
                "local_port": local_port,
                "members": [],
                "route53_records": r53,
                "target_id": cid,
            }
            inv["docdb_clusters"].append(rec)
            doc_map[cid] = rec

        try:
            inst = aws_cli_json(profile, region, ["docdb", "describe-db-instances"], log_cb)
            for d in inst.get("DBInstances", []):
                dbid = d.get("DBInstanceIdentifier")
                clid = d.get("DBClusterIdentifier")
                if not dbid or not clid:
                    continue
                if clid in doc_map:
                    raw_ep = d.get("Endpoint") or ""
                    if isinstance(raw_ep, dict):
                        ep = (raw_ep.get("Address") or raw_ep.get("Endpoint") or "").strip()
                        port = int(raw_ep.get("Port") or d.get("Port") or 27017)
                    else:
                        ep = str(raw_ep or "").strip()
                        port = int(d.get("Port") or 27017)
                    st = d.get("DBInstanceStatus") or d.get("Status") or "unknown"
                    doc_map[clid]["members"].append({
                        "name": dbid,
                        "endpoint": ep,
                        "remote_port": port,
                        "local_port": stable_local_port(f"docdb_instance|{profile}|{region}|{dbid}|{port}"),
                        "status": st,
                        "engine": "docdb",
                        "aws_target": dbid,
                        "target_type": "docdb_cluster_instance",
                        "route53_records": match_route53_records(route53_records, ep),
                        "can_forward": st.lower() in ("available", "running"),
                    })
        except Exception:
            pass

    except Exception:
        pass

    apply_external_dns_mappings(inv, log_cb)

    return inv


def write_inventory_csv(inv: Dict[str, Any], csv_path: str):
    rows = []

    for it in inv["ec2"]:
        rows.append(
            {
                "type": "ec2",
                "profile": inv["profile"],
                "region": inv["region"],
                "id": it["instance_id"],
                "name": it["name"],
                "endpoint": "",
                "port": it["remote_port"],
                "local_port": it["local_port"],
                "status": it["state"],
                "engine": it["platform"],
                "route53": "|".join(it.get("route53_records") or []),
            }
        )

    for it in inv["rds_instances"]:
        rows.append(
            {
                "type": "rds_instance",
                "profile": inv["profile"],
                "region": inv["region"],
                "id": it["db_id"],
                "name": it["name"],
                "endpoint": it["endpoint"],
                "port": it["port"],
                "local_port": it["local_port"],
                "status": it["status"],
                "engine": it["engine"],
                "route53": "|".join(it.get("route53_records") or []),
            }
        )

    for it in inv["rds_clusters"]:
        rows.append(
            {
                "type": "rds_cluster",
                "profile": inv["profile"],
                "region": inv["region"],
                "id": it["cluster_id"],
                "name": it["name"],
                "endpoint": it["endpoint"],
                "port": it["port"],
                "local_port": it["local_port"],
                "status": it["status"],
                "engine": it["engine"],
                "route53": "|".join(it.get("route53_records") or []),
            }
        )

    for it in inv.get("docdb_clusters", []):
        rows.append(
            {
                "type": "docdb_cluster",
                "profile": inv["profile"],
                "region": inv["region"],
                "id": it["cluster_id"],
                "name": it["name"],
                "endpoint": it["endpoint"],
                "port": it["port"],
                "local_port": it["local_port"],
                "status": it["status"],
                "engine": it["engine"],
                "route53": "|".join(it.get("route53_records") or []),
            }
        )

    fieldnames = ["type", "profile", "region", "id", "name", "endpoint", "port", "local_port", "status", "engine", "route53"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------------------
# DB Upserts
# --------------------------------------------------------------------------------------

def upsert_profile(profile):
    con = db_conn()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO Profile(
              ProfileName, AuthType, SsoStartUrl, SsoRegion, AccountId, RoleName,
              DefaultRegion, OutputFormat, HasCredentials, IsEnabled
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                profile.name,
                profile.auth_type,
                profile.sso_start_url or "",
                profile.sso_region or "",
                profile.account_id or "",
                profile.role_name or "",
                profile.region or "us-east-1",
                "json",
                1 if profile.has_credentials else 0,
                1,
            ),
        )
        con.commit()
    finally:
        con.close()


def _is_jumpbox_name(name: str) -> bool:
    nm = (name or "").lower()
    jump_keywords = [
        "jump", "jumpbox", "bastion", "bast",
        "ssm", "ssmhost", "ssm-host",
        "gateway", "gw", "proxy", "socks", "tunnel", "vpn", "connector",
    ]
    return any(k in nm for k in jump_keywords)


# Centralized special EC2 classification for ATLAS V2.0.
# Unmatched instances intentionally fall through to generic EC2 / Shell.
EC2_NAME_CLASSIFICATION = {
    "Neo4j Web URL": {"port": 8080, "view": "DBA", "tokens": ("neo4j",)},
    "RabbitMQ Web URL": {"port": 8080, "view": "WEB", "tokens": ("rabbitmq", "rabbit-mq", "rabbit", "rmqdata", "rmqlog", "rmglog", "rmq-server", "rmqserver")},
    "KPOW Web URL": {"port": 2190, "view": "WEB", "tokens": ("kpow", "k-pow", "kpw")},
    "Grafana/Prometheus Web URL": {"port": 3000, "view": "WEB", "tokens": ("grafana", "prometheus")},
    "Kibana Web URL": {"port": 8080, "view": "WEB", "requires_compact": ("cwdata",), "tokens": ("elk-kib", "elk_kib", "kibana")},
}


def _webui_groups_and_ports(name: str) -> List[Tuple[str, int]]:
    """Return one special group/port for a matching EC2 Name tag."""
    nm = (name or "").lower().strip()
    compact = re.sub(r"[^a-z0-9]+", "", nm)

    for group_name in (
        "Neo4j Web URL",
        "KPOW Web URL",
        "Kibana Web URL",
        "Grafana/Prometheus Web URL",
        "RabbitMQ Web URL",
    ):
        rule = EC2_NAME_CLASSIFICATION[group_name]
        required = tuple(rule.get("requires_compact") or ())
        if required and not all(token in compact for token in required):
            continue

        matched = any(token in nm for token in tuple(rule.get("tokens") or ()))
        if group_name == "KPOW Web URL" and not matched:
            matched = bool(re.search(r"(^|[^a-z0-9])(kpow|k-pow|kpw)([^a-z0-9]|$)", nm))
        if group_name == "RabbitMQ Web URL" and not matched:
            matched = bool(re.search(r"(^|[^a-z0-9])rmq([^a-z0-9]|$)", nm))

        if matched:
            return [(group_name, int(rule["port"]))]

    return []

def _webui_group_and_port(name: str) -> Tuple[Optional[str], Optional[int]]:
    groups = _webui_groups_and_ports(name)
    if groups:
        return groups[0]
    return None, None


def _is_webui_name(name: str) -> bool:
    group, _ = _webui_group_and_port(name)
    return bool(group)


def _is_redis_name(name: str) -> bool:
    nm = (name or "").lower()

    redis_keywords = [
        "redis",
        "valkey",
        "cache",
        "elasticache",
    ]

    return any(k in nm for k in redis_keywords)


def _is_kes_name(name: str) -> bool:
    nm = (name or "").lower()
    return (
        "kes" in nm
        or "key-encryption" in nm
        or "keyencryption" in nm
    )


def upsert_inventory_to_targets(inv: Dict[str, Any], profiles: List[AwsProfile]):
    now = _utc_now_iso_full()
    prof = next((p for p in profiles if p.name == inv["profile"]), None)
    env_from_profile = (prof.environment.upper() if prof else "OTHER")

    con = db_conn()
    try:
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute("BEGIN;")

        # Full rebuild for selected profile + region.
        # Fixes stale/deleted AWS resources still showing in UI.
        con.execute(
            """
            DELETE FROM FavoriteTarget
            WHERE ProfileName = ?
              AND Region = ?
              AND TargetId IN (
                SELECT TargetId FROM Target
                WHERE ProfileName = ?
                  AND Region = ?
              )
            """,
            (inv["profile"], inv["region"], inv["profile"], inv["region"]),
        )

        con.execute(
            """
            DELETE FROM Target
            WHERE ProfileName = ?
              AND Region = ?
            """,
            (inv["profile"], inv["region"]),
        )

        for it in inv["ec2"]:
            env = infer_env(it["name"]) if it["name"] else env_from_profile

            groups = []
            web_groups = _webui_groups_and_ports(it["name"])

            if _is_jumpbox_name(it["name"]):
                groups.append(("Jumpbox", it["remote_port"]))
            elif web_groups:
                groups.extend(web_groups)
            elif _is_redis_name(it["name"]):
                groups.append(("Redis", 6379))
            elif _is_kes_name(it["name"]):
                groups.append(("KES", it["remote_port"]))
            else:
                groups.append(("EC2", it["remote_port"]))

            for group, remote_port in groups:
                target_id = f"ec2|{inv['profile']}|{inv['region']}|{it['instance_id']}|{group}|{remote_port}"
                local_port = stable_local_port(target_id)

                con.execute(
                    """
                    INSERT OR REPLACE INTO Target(
                      TargetId, ProfileName, DisplayName, TargetType, AwsTarget,
                      RemoteHost, RemotePort, LocalPort, Env, Region, GroupTitle,
                      Description, IsEnabled, SortOrder, CreatedAtUtc
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        target_id,
                        inv["profile"],
                        it["name"] or it["instance_id"],
                        "ec2",
                        it["instance_id"],
                        it.get("private_dns") or it.get("private_ip") or None,
                        int(remote_port),
                        int(local_port),
                        env,
                        inv["region"],
                        group,
                        _desc_join({
                            "state": it.get("state"),
                            "platform": it.get("platform"),
                            "private_ip": it.get("private_ip"),
                            "private_dns": it.get("private_dns"),
                            "public_ip": it.get("public_ip"),
                            "public_dns": it.get("public_dns"),
                            "route53": it.get("route53_records") or [],
                        }),
                        1,
                        0,
                        now,
                    ),
                )

        for it in inv["rds_instances"]:
            env = infer_env(it["name"]) if it["name"] else env_from_profile
            group = it.get("tech") or "Other DB"
            target_id = f"rds_instance|{inv['profile']}|{inv['region']}|{it['db_id']}"

            con.execute(
                """
                INSERT OR REPLACE INTO Target(
                  TargetId, ProfileName, DisplayName, TargetType, AwsTarget,
                  RemoteHost, RemotePort, LocalPort, Env, Region, GroupTitle,
                  Description, IsEnabled, SortOrder, CreatedAtUtc
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    target_id,
                    inv["profile"],
                    it["name"] or it["db_id"],
                    "rds_instance",
                    it["db_id"],
                    it["endpoint"] or None,
                    int(it["port"]),
                    int(it["local_port"]),
                    env,
                    inv["region"],
                    group,
                    _desc_join({
                        "engine": it.get("engine"),
                        "status": it.get("status"),
                        "route53": it.get("route53_records") or [],
                    }),
                    1,
                    0,
                    now,
                ),
            )

        for it in inv["rds_clusters"]:
            env = infer_env(it["name"]) if it["name"] else env_from_profile
            group = it.get("cluster_group") or "Cluster"
            members = it.get("members") or []
            target_id = f"rds_cluster|{inv['profile']}|{inv['region']}|{it['cluster_id']}"

            con.execute(
                """
                INSERT OR REPLACE INTO Target(
                  TargetId, ProfileName, DisplayName, TargetType, AwsTarget,
                  RemoteHost, RemotePort, LocalPort, Env, Region, GroupTitle,
                  Description, IsEnabled, SortOrder, CreatedAtUtc
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    target_id,
                    inv["profile"],
                    it["name"] or it["cluster_id"],
                    "rds_cluster",
                    it["cluster_id"],
                    it["endpoint"] or None,
                    int(it["port"]),
                    int(it["local_port"]),
                    env,
                    inv["region"],
                    group,
                    _desc_join({
                        "engine": it.get("engine"),
                        "status": it.get("status"),
                        "members": it.get("member_names") or [m.get("name") for m in members if isinstance(m, dict)],
                        "route53": it.get("route53_records") or [],
                        "members_json": members,
                    }),
                    1,
                    0,
                    now,
                ),
            )

        for it in inv.get("docdb_clusters", []):
            env = infer_env(it["name"]) if it["name"] else env_from_profile
            group = "DocDB Cluster"
            members = it.get("members") or []
            target_id = f"docdb_cluster|{inv['profile']}|{inv['region']}|{it['cluster_id']}"

            con.execute(
                """
                INSERT OR REPLACE INTO Target(
                  TargetId, ProfileName, DisplayName, TargetType, AwsTarget,
                  RemoteHost, RemotePort, LocalPort, Env, Region, GroupTitle,
                  Description, IsEnabled, SortOrder, CreatedAtUtc
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    target_id,
                    inv["profile"],
                    it["name"] or it["cluster_id"],
                    "docdb_cluster",
                    it["cluster_id"],
                    it["endpoint"] or None,
                    27017,
                    int(it["local_port"]),
                    env,
                    inv["region"],
                    group,
                    _desc_join({
                        "engine": "docdb",
                        "status": it.get("status"),
                        "members": [m.get("name") for m in members if isinstance(m, dict)] if members and isinstance(members[0], dict) else members,
                        "route53": it.get("route53_records") or [],
                        "members_json": members,
                    }),
                    1,
                    0,
                    now,
                ),
            )

        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
# --------------------------------------------------------------------------------------
# Catalog JSON
# --------------------------------------------------------------------------------------

def _parse_kv(desc: str, key: str) -> str:
    if not desc:
        return ""
    m = re.search(rf"\b{re.escape(key)}=([A-Za-z0-9._:-]+)", desc)
    return m.group(1) if m else ""


def _parse_list_kv(desc: str, key: str) -> List[str]:
    if not desc:
        return []
    m = re.search(rf"\b{re.escape(key)}=([^;]+)", desc)
    if not m:
        return []
    val = (m.group(1) or "").strip()
    if not val:
        return []
    # Route53 values are stored pipe-separated by _desc_join.
    parts = re.split(r"[|,]", val)
    return [x.strip() for x in parts if x.strip()]


def _parse_json_kv(desc: str, key: str) -> Any:
    if not desc:
        return None
    m = re.search(rf"\b{re.escape(key)}=(.*?)(?:; [A-Za-z0-9_]+=|$)", desc)
    if not m:
        return None
    raw = (m.group(1) or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _env_match_tokens(env: str) -> List[str]:
    e = (env or "").lower().strip()
    if not e:
        return []
    if e in ("stage", "stg"):
        return ["stage", "stg", "demo"]
    if e == "demo":
        return ["demo", "stage", "stg"]
    if e == "perf":
        return ["perf", "stage", "stg"]
    if e == "prod":
        return ["prod", "prd"]
    if e == "vnv":
        return ["vnv"]
    if e == "preprod":
        return ["preprod"]
    if e == "qa":
        return ["qa"]
    if e == "dev":
        return ["dev"]
    return [e]


def _load_catalog_for(profile: str, region: str) -> Dict[str, Any]:
    path = catalog_json_path_for(profile, region)

    # New per-profile/per-region catalog
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cat = json.load(f)
            if cat.get("profile") == profile and cat.get("region") == region:
                return cat
        except Exception:
            return {}

    # Legacy fallback for old catalog.json
    if os.path.exists(CATALOG_JSON_PATH):
        try:
            with open(CATALOG_JSON_PATH, "r", encoding="utf-8") as f:
                cat = json.load(f)
            if cat.get("profile") == profile and cat.get("region") == region:
                return cat
        except Exception:
            return {}

    return {}


def _get_jumpboxes_from_catalog(profile: str, region: str) -> List[Dict[str, Any]]:
    cat = _load_catalog_for(profile, region)
    jump_list = (cat.get("ec2_grouped", {}) or {}).get("Jumpbox", []) or []
    out = []
    for j in jump_list:
        out.append(
            {
                "name": j.get("name") or "",
                "instance_id": (j.get("instance_id") or j.get("aws_target") or "").strip(),
                "env": (j.get("env") or "OTHER").upper(),
                "state": (j.get("state") or "unknown").lower(),
            }
        )
    seen = set()
    uniq = []
    for x in out:
        if not x["instance_id"]:
            continue
        k = x["instance_id"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


def _jumpbox_exists_in_catalog(profile: str, region: str, jumpbox_id: str) -> bool:
    if not jumpbox_id:
        return False
    for j in _get_jumpboxes_from_catalog(profile, region):
        if (j.get("instance_id") or "").strip() == jumpbox_id.strip():
            return True
    return False


def pick_jumpbox_instance_id(profile: str, region: str, env: str) -> Optional[str]:
    cat = _load_catalog_for(profile, region)
    jump_list = (cat.get("ec2_grouped", {}) or {}).get("Jumpbox", []) or []
    if not jump_list:
        return None

    tokens = _env_match_tokens(env)

    def score(j) -> int:
        nm = (j.get("name") or "").lower()
        s = 0
        for t in tokens:
            if re.search(rf"(^|[^a-z0-9]){re.escape(t)}([^a-z0-9]|$)", nm):
                s += 100 if t == tokens[0] else 50
        if "ssm" in nm:
            s += 10
        if "jumpbox" in nm or "bastion" in nm or "jump" in nm:
            s += 5
        return s

    best = max(jump_list, key=score)
    if score(best) > 0:
        return (best.get("instance_id") or best.get("aws_target") or "").strip() or None

    j0 = jump_list[0]
    return (j0.get("instance_id") or j0.get("aws_target") or "").strip() or None


def pick_docdb_jumpbox_instance_id(profile: str, region: str, env: str) -> Optional[str]:
    cat = _load_catalog_for(profile, region)
    jump_list = (cat.get("ec2_grouped", {}) or {}).get("Jumpbox", []) or []
    if not jump_list:
        return None

    tokens = _env_match_tokens(env)

    def score(j) -> int:
        nm = (j.get("name") or "").lower()
        if "docdb" not in nm:
            return 0
        s = 500
        if "jumpbox" in nm or "bastion" in nm or re.search(r"\bjump\b", nm):
            s += 200
        for t in tokens:
            if re.search(rf"(^|[^a-z0-9]){re.escape(t)}([^a-z0-9]|$)", nm):
                s += 50
        return s

    best = max(jump_list, key=score)
    if score(best) > 0:
        return (best.get("instance_id") or best.get("aws_target") or "").strip() or None
    return None


def pick_jumpbox_for_target(profile: str, region: str, env: str, target_type: str) -> Optional[str]:
    """
    Behavior:
    1) If user saved a preferred jumpbox -> use it (but only if it still exists in catalog)
    2) Else use existing auto-selection logic
    """
    tt = (target_type or "db").lower().strip()
    env_up = (env or "OTHER").upper()

    preferred = get_jumpbox_preference(profile, region, env_up, tt)
    if preferred and _jumpbox_exists_in_catalog(profile, region, preferred):
        return preferred

    if tt == "docdb_cluster":
        jb = pick_docdb_jumpbox_instance_id(profile, region, env_up)
        if jb:
            return jb
    return pick_jumpbox_instance_id(profile, region, env_up)


def targets_to_catalog_json(profile: str, region: str) -> Dict[str, Any]:
    con = db_conn()
    try:
        rows = con.execute(
            """
            SELECT * FROM Target
            WHERE ProfileName=? AND IFNULL(Region,'')=?
              AND IsEnabled=1
            """,
            (profile, region),
        ).fetchall()
    finally:
        con.close()

    favorite_ids = get_favorite_target_ids(profile, region)

    db_grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    ec2_grouped: Dict[str, List[Dict[str, Any]]] = {
        "Neo4j Web URL": [],
        "RabbitMQ Web URL": [],
        "KPOW Web URL": [],
        "Grafana/Prometheus Web URL": [],
        "Kibana Web URL": [],
        "Redis": [],
        "KES": [],
        "Jumpbox": [],
        "EC2": [],
    }

    for r in rows:
        ttype = (r["TargetType"] or "").lower()
        env = (r["Env"] or "OTHER").upper()
        group = r["GroupTitle"] or "Other"
        desc = r["Description"] or ""

        if ttype in ("rds_instance", "rds_cluster", "docdb_cluster"):
            status = _parse_kv(desc, "status") or "unknown"
            engine = _parse_kv(desc, "engine") or ""
            endpoint = (r["RemoteHost"] or "").strip()
            remote_port = int(r["RemotePort"] or 0)

            can_forward = (status.lower() in ("available", "running"))

            db_grouped.setdefault(group, {}).setdefault(env, []).append(
                {
                    "target_id": r["TargetId"],
                    "is_favorite": r["TargetId"] in favorite_ids,
                    "name": r["DisplayName"],
                    "endpoint": endpoint,
                    "remote_port": remote_port,
                    "local_port": int(r["LocalPort"] or 0),
                    "aws_target": r["AwsTarget"],
                    "target_type": r["TargetType"],
                    "env": env,
                    "status": status,
                    "engine": engine,
                    "members": _parse_json_kv(desc, "members_json") or [],
                    "route53_records": _parse_list_kv(desc, "route53"),
                    "can_forward": can_forward,
                }
            )

        elif ttype == "ec2":
            group_title = r["GroupTitle"] or "EC2"
            bucket = group_title if group_title in ec2_grouped else "EC2"

            state = _parse_kv(desc, "state") or "unknown"
            can_forward = (state.lower() == "running")

            ec2_grouped[bucket].append(
                {
                    "target_id": r["TargetId"],
                    "is_favorite": r["TargetId"] in favorite_ids,
                    "name": r["DisplayName"],
                    "instance_id": r["AwsTarget"],
                    "remote_port": int(r["RemotePort"] or 0),
                    "local_port": int(r["LocalPort"] or 0),
                    "grafana_remote_port": 3000 if bucket == "Grafana/Prometheus Web URL" else int(r["RemotePort"] or 0),
                    "prometheus_remote_port": 9090 if bucket == "Grafana/Prometheus Web URL" else 0,
                    "prometheus_local_port": (
                        stable_local_port(
                            f"ec2|{profile}|{region}|{r['AwsTarget']}|Grafana/Prometheus Web URL|9090"
                        )
                        if bucket == "Grafana/Prometheus Web URL"
                        else 0
                    ),
                    "private_ip": _parse_kv(desc, "private_ip"),
                    "private_dns": _parse_kv(desc, "private_dns"),
                    "public_ip": _parse_kv(desc, "public_ip"),
                    "public_dns": _parse_kv(desc, "public_dns"),
                    "route53_records": _parse_list_kv(desc, "route53"),
                    "aws_target": r["AwsTarget"],
                    "target_type": r["TargetType"],
                    "env": env,
                    "state": state,
                    "can_forward": can_forward,
                }
            )

    return {
        "profile": profile,
        "region": region,
        "generated_utc": _utc_now_iso_full(),
        "db_grouped": db_grouped,
        "ec2_grouped": ec2_grouped,
    }


# --------------------------------------------------------------------------------------
# Update jobs
# --------------------------------------------------------------------------------------

def job_log_append(job_id: str, text: str):
    ensure_tables_exist()
    con = db_conn()
    try:
        row = con.execute("SELECT LogText FROM UpdateJob WHERE JobId=?", (job_id,)).fetchone()
        old = row["LogText"] if row else ""
        new = (old or "") + text
        now = _utc_now_iso_full()
        con.execute(
            "UPDATE UpdateJob SET LogText=?, LastUpdateUtc=? WHERE JobId=?",
            (new, now, job_id),
        )
        con.commit()
    finally:
        con.close()

    _append_job_file_log(job_id, text)


def job_set_status(job_id: str, status: str, pid: Optional[int] = None):
    ensure_tables_exist()
    con = db_conn()
    try:
        now = _utc_now_iso_full()
        if pid is None:
            con.execute(
                "UPDATE UpdateJob SET Status=?, LastUpdateUtc=? WHERE JobId=?",
                (status, now, job_id),
            )
        else:
            con.execute(
                "UPDATE UpdateJob SET Status=?, Pid=?, LastUpdateUtc=? WHERE JobId=?",
                (status, int(pid), now, job_id),
            )
        con.commit()
    finally:
        con.close()


def start_update_job(profile: str, region: str) -> str:
    ensure_tables_exist()
    job_id = str(uuid.uuid4())
    con = db_conn()
    try:
        con.execute(
            """
            INSERT INTO UpdateJob(JobId, ProfileName, Region, Status, Pid, CreatedAtUtc, LastUpdateUtc, LogText)
            VALUES(?,?,?,?,?,datetime('now'),datetime('now'),'')
            """,
            (job_id, profile, region, "starting", 0),
        )
        con.commit()
    finally:
        con.close()

    def worker():
        try:
            job_set_status(job_id, "running", os.getpid())

            def log_cb(s: str):
                job_log_append(job_id, s)

            inv = build_inventory(profile, region, log_cb)

            ts = _utc_now().strftime("%Y%m%d_%H%M%S")
            csv_path = os.path.join(EXPORT_DIR, f"aws_inventory_{profile}_{region}_{ts}.csv")
            write_inventory_csv(inv, csv_path)

            profiles = load_profiles()
            for p in profiles:
                upsert_profile(p)

            upsert_inventory_to_targets(inv, profiles)

            catalog = targets_to_catalog_json(profile, region)

            profile_catalog_path = catalog_json_path_for(profile, region)
            with open(profile_catalog_path, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2)

            # Optional legacy copy, useful while migration is in progress
            with open(CATALOG_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2)

            job_set_status(job_id, "completed")
        except Exception as e:
            job_set_status(job_id, "failed")
            job_log_append(job_id, "\nFAILED:\n" + str(e) + "\n")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return job_id


def get_latest_job(profile: str, region: str) -> Optional[Dict[str, Any]]:
    ensure_tables_exist()
    con = db_conn()
    try:
        row = con.execute(
            """
            SELECT * FROM UpdateJob
            WHERE ProfileName=? AND Region=?
            ORDER BY datetime(LastUpdateUtc) DESC, datetime(CreatedAtUtc) DESC
            LIMIT 1
            """,
            (profile, region),
        ).fetchone()
        if not row:
            return None
        return {
            "job_id": row["JobId"],
            "status": row["Status"],
            "pid": row["Pid"],
            "last_update": row["LastUpdateUtc"],
        }
    finally:
        con.close()


# --------------------------------------------------------------------------------------
# Favorites API
# --------------------------------------------------------------------------------------

def get_favorite_target_ids(profile: str, region: str) -> set:
    ensure_tables_exist()
    con = db_conn()
    try:
        rows = con.execute(
            "SELECT TargetId FROM FavoriteTarget WHERE ProfileName=? AND Region=?",
            (profile, region),
        ).fetchall()
        return {r["TargetId"] for r in rows}
    finally:
        con.close()


def set_favorite_target(profile: str, region: str, target_id: str, favorite: bool) -> bool:
    ensure_tables_exist()
    con = db_conn()
    try:
        if favorite:
            con.execute(
                """
                INSERT OR IGNORE INTO FavoriteTarget(ProfileName, Region, TargetId, CreatedAtUtc)
                VALUES(?,?,?,?)
                """,
                (profile, region, target_id, _utc_now_iso()),
            )
        else:
            con.execute(
                "DELETE FROM FavoriteTarget WHERE ProfileName=? AND Region=? AND TargetId=?",
                (profile, region, target_id),
            )
        con.commit()
        return favorite
    finally:
        con.close()


@app.route("/api/favorites/toggle", methods=["POST"])
def api_favorites_toggle():
    data = request.get_json(silent=True) or {}
    profile = (data.get("profile") or request.form.get("profile") or "").strip()
    region = (data.get("region") or request.form.get("region") or "").strip()
    target_id = (data.get("target_id") or request.form.get("target_id") or "").strip()
    favorite = bool(data.get("favorite"))

    if not profile or not region or not target_id:
        return jsonify({"ok": False, "error": "Missing profile/region/target_id"}), 400

    fav = set_favorite_target(profile, region, target_id, favorite)
    return jsonify({"ok": True, "target_id": target_id, "is_favorite": fav})


@app.route("/api/diagnostics", methods=["GET"])
def api_diagnostics():
    profile = request.args.get("profile", "").strip()
    region = request.args.get("region", "").strip() or "us-east-1"

    try:
        code, out, err = run_cmd(aws_cmd_base() + ["--version"], timeout=10)
        aws_ok = code == 0
        aws_version = (out or err or "Unavailable").strip()
    except Exception as e:
        aws_ok, aws_version = False, str(e)

    try:
        code, out, err = run_cmd(["session-manager-plugin", "--version"], timeout=10)
        plugin_ok = code == 0
        plugin_version = (out or err or "Unavailable").strip()
    except Exception as e:
        plugin_ok, plugin_version = False, str(e)

    identity = {}
    auth_message = "Profile not selected"
    if profile:
        auth = check_aws_profile_auth(profile, region if region.upper() != "ALL" else "us-east-1")
        identity = auth.get("identity") or {}
        auth_message = auth.get("message") or ("ok" if auth.get("ok") else "failed")

    if profile and region.upper() != "ALL":
        cat = catalog_status(profile, region)
    else:
        cat = {"exists": True, "fresh": True, "status": "Aggregated",
               "age_text": "Multiple regions", "last_refresh_utc": "", "path": str(APP_DATA_DIR)}

    counts = {"database": 0, "web": 0, "ec2_shell": 0, "total": 0}
    try:
        if profile and region.upper() != "ALL":
            cached = _load_catalog_for(profile, region) or {}
            dbg = cached.get("db_grouped", {}) or {}
            ecg = cached.get("ec2_grouped", {}) or {}
            counts["database"] = sum(len(items or []) for envmap in dbg.values() for items in (envmap or {}).values())
            web_groups = {"Neo4j Web URL", "RabbitMQ Web URL", "KPOW Web URL", "Grafana/Prometheus Web URL", "Kibana Web URL"}
            counts["web"] = sum(len(items or []) for tech, items in ecg.items() if tech in web_groups)
            counts["ec2_shell"] = sum(len(items or []) for tech, items in ecg.items() if tech not in web_groups)
            counts["total"] = counts["database"] + counts["web"] + counts["ec2_shell"]
    except Exception:
        pass

    with ACTIVE_TUNNELS_LOCK:
        active_count = len(ACTIVE_TUNNELS)

    return jsonify({
        "ok": True,
        "app": {"name": APP_TITLE, "version": APP_VERSION},
        "profile": profile, "region": region,
        "account_id": identity.get("Account", ""),
        "caller_arn": identity.get("Arn", ""),
        "auth_message": auth_message,
        "aws_cli": {"ok": aws_ok, "version": aws_version},
        "session_manager_plugin": {"ok": plugin_ok, "version": plugin_version},
        "paths": {
            "app_data": str(APP_DATA_DIR), "catalog_db": DB_PATH,
            "catalog_json": catalog_json_path_for(profile, region) if profile and region.upper() != "ALL" else "",
            "active_tunnels": ACTIVE_TUNNELS_STATE_PATH, "logs": str(LOG_DIR),
        },
        "catalog": cat, "counts": counts, "active_tunnels": active_count,
    })


# --------------------------------------------------------------------------------------
# Jumpbox API (for modal)
# --------------------------------------------------------------------------------------


def stop_all_active_tunnels(profile: str = "", region: str = "") -> Dict[str, Any]:
    """Stop tracked SSM child processes and remove their persisted ATLAS session records."""
    with ACTIVE_TUNNELS_LOCK:
        rows = [dict(x) for x in ACTIVE_TUNNELS.values()]

    if profile:
        rows = [x for x in rows if x.get("profile") == profile]
    if region:
        rows = [x for x in rows if x.get("region") == region]

    results = []
    ids_to_remove = []
    for rec in rows:
        tunnel_id = str(rec.get("id") or "")
        pid = int(rec.get("pid") or 0)
        if pid:
            ok, msg = _stop_pid(pid)
        else:
            ok, msg = True, "No PID; session metadata removed"
        results.append({
            "id": tunnel_id, "pid": pid,
            "label": rec.get("label") or rec.get("target") or rec.get("type") or "session",
            "ok": bool(ok), "message": msg,
        })
        if tunnel_id:
            ids_to_remove.append(tunnel_id)

    with ACTIVE_TUNNELS_LOCK:
        for tunnel_id in ids_to_remove:
            ACTIVE_TUNNELS.pop(tunnel_id, None)
        _persist_active_tunnels_locked()

    return {
        "ok": all(x["ok"] for x in results) if results else True,
        "count": len(results),
        "stopped": sum(1 for x in results if x["ok"]),
        "results": results,
    }


@app.route("/api/ssm/active-tunnels", methods=["GET"])
def api_active_tunnels():
    profile = request.args.get("profile", "").strip()
    region = request.args.get("region", "").strip()
    rows = list_active_tunnels(profile, region)
    return jsonify({"ok": True, "count": len(rows), "tunnels": rows})


@app.route("/api/ssm/stop-tunnel", methods=["POST"])
def api_stop_tunnel():
    data = request.get_json(silent=True) or {}
    tunnel_id = (data.get("tunnel_id") or request.form.get("tunnel_id") or "").strip()
    if not tunnel_id:
        return jsonify({"ok": False, "message": "Missing tunnel_id"}), 400

    with ACTIVE_TUNNELS_LOCK:
        rec = ACTIVE_TUNNELS.get(tunnel_id)
    if not rec:
        return jsonify({"ok": False, "message": "Tunnel not found"}), 404

    ok, msg = _stop_pid(int(rec.get("pid") or 0))
    with ACTIVE_TUNNELS_LOCK:
        ACTIVE_TUNNELS.pop(tunnel_id, None)
        _persist_active_tunnels_locked()
    return jsonify({"ok": ok, "message": msg, "tunnel_id": tunnel_id})



@app.route("/api/ssm/stop-all", methods=["POST"])
def api_stop_all_tunnels():
    data = request.get_json(silent=True) or {}
    profile = (data.get("profile") or request.form.get("profile") or "").strip()
    region = (data.get("region") or request.form.get("region") or "").strip()
    result = stop_all_active_tunnels(profile=profile, region=region)
    return jsonify(result), (200 if result.get("ok") else 207)


@app.route("/api/ssm/restart-tunnel", methods=["POST"])
def api_restart_tunnel():
    data = request.get_json(silent=True) or {}
    tunnel_id = (data.get("tunnel_id") or request.form.get("tunnel_id") or "").strip()
    if not tunnel_id:
        return jsonify({"ok": False, "message": "Missing tunnel_id"}), 400

    ok, msg, rec = _restart_tunnel_record(tunnel_id, reason="manual")
    return jsonify({"ok": ok, "message": msg, "tunnel": _decorate_tunnel(rec) if rec else {}}), (200 if ok else 400)


@app.route("/api/ssm/keepalive", methods=["POST"])
def api_ssm_keepalive():
    data = request.get_json(silent=True) or {}
    profile = (data.get("profile") or request.form.get("profile") or "").strip()
    region = (data.get("region") or request.form.get("region") or "").strip()
    tunnel_id = (data.get("tunnel_id") or request.form.get("tunnel_id") or "").strip()

    with ACTIVE_TUNNELS_LOCK:
        rows = list(ACTIVE_TUNNELS.values())

    if tunnel_id:
        rows = [x for x in rows if x.get("id") == tunnel_id]
    else:
        if profile:
            rows = [x for x in rows if x.get("profile") == profile]
        if region:
            rows = [x for x in rows if x.get("region") == region]

    results = []
    now = _utc_now_iso_full()
    for rec in rows:
        local_port = int(rec.get("local_port") or 0)
        if local_port > 0:
            ok, msg = keepalive_local_port(local_port)
        else:
            # SSM shell sessions do not have a localhost port to probe.
            ok, msg = (_pid_is_running(int(rec.get("pid") or 0)), "shell pid running")

        with ACTIVE_TUNNELS_LOCK:
            if rec.get("id") in ACTIVE_TUNNELS:
                ACTIVE_TUNNELS[rec["id"]]["last_keepalive_utc"] = now
                ACTIVE_TUNNELS[rec["id"]]["last_status"] = "ok" if ok else msg[:200]
                _persist_active_tunnels_locked()
        results.append({
            "id": rec.get("id"),
            "type": rec.get("type"),
            "target": rec.get("target"),
            "local_port": rec.get("local_port"),
            "remote_port": rec.get("remote_port"),
            "ok": ok,
            "message": msg[:300],
        })

    return jsonify({"ok": True, "count": len(results), "results": results})


@app.route("/api/jumpboxes", methods=["GET"])
def api_jumpboxes():
    profile = (request.args.get("profile") or "").strip()
    region = (request.args.get("region") or "").strip()
    env = (request.args.get("env") or "OTHER").strip().upper()

    if not profile or not region:
        return jsonify({"ok": False, "error": "Missing profile/region"}), 400

    jumpboxes = _get_jumpboxes_from_catalog(profile, region)
    tokens = _env_match_tokens(env)

    def env_score(j):
        nm = (j.get("name") or "").lower()
        s = 0
        for t in tokens:
            if re.search(rf"(^|[^a-z0-9]){re.escape(t)}([^a-z0-9]|$)", nm):
                s += 100 if t == tokens[0] else 50
        if (j.get("state") or "") == "running":
            s += 10
        if "jumpbox" in nm or "bastion" in nm or "jump" in nm or "ssm" in nm:
            s += 5
        return s

    jumpboxes_sorted = sorted(jumpboxes, key=env_score, reverse=True)
    return jsonify({"ok": True, "jumpboxes": jumpboxes_sorted})


@app.route("/api/jumpbox/get", methods=["GET"])
def api_jumpbox_get():
    """
    Returns the EFFECTIVE jumpbox (saved if exists & valid, else auto) + source.
    This is what the UI displays as the current jumpbox.
    """
    profile = (request.args.get("profile") or "").strip()
    region = (request.args.get("region") or "").strip()
    env = (request.args.get("env") or "OTHER").strip().upper()
    target_type = (request.args.get("target_type") or "db").strip().lower()

    if not profile or not region:
        return jsonify({"ok": False, "error": "Missing profile/region"}), 400

    saved = get_jumpbox_preference(profile, region, env, target_type) or ""
    if saved and _jumpbox_exists_in_catalog(profile, region, saved):
        return jsonify({"ok": True, "effective_jumpbox_id": saved, "source": "saved"})

    auto = pick_jumpbox_for_target(profile, region, env, target_type) or ""
    # Note: pick_jumpbox_for_target already validates saved existence and falls back.
    return jsonify({"ok": True, "effective_jumpbox_id": auto, "source": "auto"})


@app.route("/api/jumpbox/set", methods=["POST"])
def api_jumpbox_set():
    data = request.get_json(force=True, silent=True) or {}
    profile = (data.get("profile") or "").strip()
    region = (data.get("region") or "").strip()
    env = (data.get("env") or "OTHER").strip().upper()
    target_type = (data.get("target_type") or "db").strip().lower()
    jumpbox_id = (data.get("jumpbox_id") or "").strip()

    if not profile or not region or not jumpbox_id:
        return jsonify({"ok": False, "error": "Missing profile/region/jumpbox_id"}), 400

    # Optional: prevent saving an invalid jumpbox id (not in catalog)
    if not _jumpbox_exists_in_catalog(profile, region, jumpbox_id):
        return jsonify({"ok": False, "error": "Selected jumpbox not found in current catalog. Please refresh catalog and try again."}), 400

    set_jumpbox_preference(profile, region, env, target_type, jumpbox_id)
    return jsonify({"ok": True})



@app.route("/cluster-members", methods=["GET"])
def cluster_members():
    profile = (request.args.get("profile") or "").strip()
    region = (request.args.get("region") or "").strip() or "us-east-1"
    cluster_id = (request.args.get("cluster_id") or "").strip()
    target_type = (request.args.get("target_type") or "").strip()
    env = (request.args.get("env") or "OTHER").strip().upper()

    if not profile or not region or not cluster_id:
        return "Missing profile/region/cluster_id", 400

    cat = _load_catalog_for(profile, region)
    found = None
    for _group, envmap in (cat.get("db_grouped", {}) or {}).items():
        for _env, items in (envmap or {}).items():
            for it in items or []:
                if (it.get("aws_target") or "") == cluster_id:
                    found = it
                    env = (it.get("env") or env or "OTHER").upper()
                    target_type = target_type or (it.get("target_type") or "")
                    break
            if found:
                break
        if found:
            break

    if not found:
        return f"Cluster not found in catalog: {html.escape(cluster_id)}", 404

    members = found.get("members") or []
    title = found.get("name") or cluster_id
    rows = []
    for m in members:
        if not isinstance(m, dict):
            continue
        name = html.escape(str(m.get("name") or ""))
        endpoint_raw = str(m.get("endpoint") or "")
        route53_records = m.get("route53_records") or []
        display_endpoint_raw = str(route53_records[0]) if route53_records else endpoint_raw
        endpoint = html.escape(display_endpoint_raw)
        status = html.escape(str(m.get("status") or "unknown"))
        remote_port = int(m.get("remote_port") or found.get("remote_port") or 0)
        local_port = int(m.get("local_port") or 0)
        can_forward = bool(m.get("can_forward", True)) and bool(endpoint_raw) and remote_port > 0 and local_port > 0
        disabled = "" if can_forward else "disabled title='Instance is not available/running or endpoint is missing'"
        aws_target = html.escape(str(m.get("aws_target") or m.get("name") or ""))
        rows.append(f'''
          <div class="member-card">
            <div class="member-head">
              <div>
                <div class="member-title">{name}</div>
                <div class="muted mono">{endpoint or "Endpoint not found"}</div>
              </div>
              <span class="pill">{status}</span>
            </div>
            <div class="kv"><span>Remote Port</span><b>{remote_port}</b></div>
            <div class="kv"><span>Local Port</span><b>{local_port}</b></div>
            <form method="post" action="/ssm-forward">
              <input type="hidden" name="profile" value="{html.escape(profile)}">
              <input type="hidden" name="region" value="{html.escape(region)}">
              <input type="hidden" name="target_type" value="{html.escape(target_type)}">
              <input type="hidden" name="aws_target" value="{aws_target}">
              <input type="hidden" name="remote_host" value="{html.escape(endpoint_raw)}">
              <input type="hidden" name="remote_port" value="{remote_port}">
              <input type="hidden" name="local_port" value="{local_port}">
              <input type="hidden" name="env" value="{html.escape(env)}">
              <input type="hidden" name="can_forward" value="{1 if can_forward else 0}">
              <button class="btn" {disabled}>SSM Port Forward Instance</button>
            </form>
          </div>
        ''')

    body = "".join(rows) if rows else "<div class='empty'>No individual instance endpoints found in catalog. Refresh catalog after applying latest main.py.</div>"
    cluster_endpoint = html.escape(str(found.get("endpoint") or ""))
    cluster_port = html.escape(str(found.get("remote_port") or ""))
    return f'''
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Cluster Members - {html.escape(title)}</title>
  <style>
    body {{ margin:0; font-family:Segoe UI,Arial,sans-serif; color:#eef6ff; background:linear-gradient(135deg,#071326,#16264a 50%,#0a3b36); }}
    .wrap {{ padding:26px; }}
    .top {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }}
    h2 {{ margin:0 0 6px 0; font-size:22px; }}
    .muted {{ color:#b8c7dd; font-size:13px; }}
    .mono {{ font-family:Consolas,monospace; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:16px; }}
    .member-card {{ background:rgba(12,27,58,.78); border:1px solid rgba(132,165,220,.35); border-radius:16px; padding:16px; box-shadow:0 12px 40px rgba(0,0,0,.25); }}
    .member-head {{ display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid rgba(255,255,255,.08); padding-bottom:12px; margin-bottom:12px; }}
    .member-title {{ font-weight:800; font-size:16px; }}
    .pill {{ background:rgba(18,130,75,.35); border:1px solid rgba(36,220,128,.45); border-radius:999px; padding:5px 10px; font-size:12px; height:max-content; }}
    .kv {{ display:flex; justify-content:space-between; gap:14px; padding:5px 0; color:#c8d6ea; font-size:13px; }}
    .btn {{ margin-top:12px; border:1px solid rgba(55,235,128,.7); border-radius:10px; color:white; background:linear-gradient(180deg,#13985f,#08713f); padding:9px 13px; cursor:pointer; }}
    .btn:disabled {{ opacity:.45; cursor:not-allowed; }}
    .empty {{ padding:18px; border:1px dashed rgba(255,255,255,.2); border-radius:14px; color:#c8d6ea; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h2>{html.escape(title)}</h2>
        <div class="muted mono">Profile: {html.escape(profile)} • Region: {html.escape(region)} • Env: {html.escape(env)}</div>
        <div class="muted mono">Cluster Endpoint: {cluster_endpoint}:{cluster_port}</div>
      </div>
    </div>
    <div class="grid">{body}</div>
  </div>
</body>
</html>
'''

# --------------------------------------------------------------------------------------
# SSM
# --------------------------------------------------------------------------------------

def _open_cmd_window(cmd: List[str], title: str = "SSM") -> int:
    """
    Start SSM/AWS CLI in background without opening a visible CMD window.
    ATLAS tracks the PID and manages stop/keepalive from the UI.
    """
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(
            cmd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            cwd=BASE_DIR,
        )
        return int(p.pid or 0)

    p = subprocess.Popen(
        cmd,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=BASE_DIR,
    )
    return int(p.pid or 0)


def _write_ssm_shell_cmd(profile: str, region: str, instance_id: str) -> str:
    r"""
    Creates a temporary .cmd launcher for interactive SSM Shell.
    This avoids PowerShell/CMD quoting problems with paths like:
    C:\Program Files\Amazon\AWSCLIV2\aws.exe
    """
    aws = aws_cmd_base()[0]
    safe_instance = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id or "instance")
    cmd_path = LOG_DIR / f"atlas_ssm_shell_{safe_instance}.cmd"
    content = (
        "@echo off\n"
        "setlocal\n"
        "cls\n"
        f"title ATLAS SSM Shell - {instance_id}\n"
        "echo Starting ATLAS SSM Shell...\n"
        f"echo Profile: {profile}\n"
        f"echo Region : {region}\n"
        f"echo Target : {instance_id}\n"
        "echo.\n"
        f"\"{aws}\" ssm start-session --profile \"{profile}\" --region \"{region}\" --target \"{instance_id}\"\n"
        "echo.\n"
        "echo SSM shell ended. Press any key to close this window.\n"
        "pause >nul\n"
    )
    cmd_path.write_text(content, encoding="utf-8")
    return str(cmd_path)


def start_ssm_shell(profile: str, region: str, instance_id: str) -> int:
    """
    Interactive shell must open a visible terminal.
    DB/Web port-forward tunnels still run hidden using _open_cmd_window().
    """
    if os.name == "nt":
        cmd_file = _write_ssm_shell_cmd(profile, region, instance_id)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        p = subprocess.Popen(
            ["cmd.exe", "/k", cmd_file],
            shell=False,
            cwd=BASE_DIR,
            creationflags=flags,
        )
        return int(p.pid or 0)

    aws = aws_cmd_base()[0]
    cmd = [
        aws,
        "ssm",
        "start-session",
        "--profile", profile,
        "--region", region,
        "--target", instance_id,
    ]
    p = subprocess.Popen(cmd, shell=False, cwd=BASE_DIR)
    return int(p.pid or 0)

def _auto_open_webui_after_delay(local_port: int, delay_seconds: int = 10, path: str = "/") -> None:
    """
    Open the browser after the SSM local port is reachable.
    Neo4j should open at /browser/, while Grafana/RabbitMQ/KPOW usually use /.
    This does not require or open a CMD window; the tunnel process remains hidden.
    """
    def _worker():
        try:
            threading.Event().wait(max(1, int(delay_seconds)))

            # Give Session Manager time to bind localhost before opening the browser.
            for _ in range(45):
                ok, _msg = keepalive_local_port(int(local_port), timeout_seconds=1)
                if ok:
                    break
                threading.Event().wait(1)

            clean_path = (path or "/").strip()
            if not clean_path.startswith("/"):
                clean_path = "/" + clean_path

            webbrowser.open(f"http://127.0.0.1:{int(local_port)}{clean_path}")
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def start_ssm_port_forward_ec2(
    profile: str,
    region: str,
    instance_id: str,
    remote_port: int,
    local_port: int,
    auto_open: bool,
    open_path: str = "/",
) -> int:
    aws = aws_cmd_base()[0]
    cmd = [
        aws, "ssm", "start-session",
        "--profile", profile,
        "--region", region,
        "--target", instance_id,
        "--document-name", "AWS-StartPortForwardingSession",
        "--parameters", f'portNumber=["{int(remote_port)}"],localPortNumber=["{int(local_port)}"]',
    ]
    pid = _open_cmd_window(cmd, title=f"SSM PF {instance_id}:{remote_port}->{local_port}")
    if auto_open:
        _auto_open_webui_after_delay(local_port, delay_seconds=10, path=open_path)
    return pid


def start_ssm_port_forward_remote_host(
    profile: str,
    region: str,
    jumpbox_instance_id: str,
    remote_host: str,
    remote_port: int,
    local_port: int,
) -> int:
    aws = aws_cmd_base()[0]
    cmd = [
        aws, "ssm", "start-session",
        "--profile", profile,
        "--region", region,
        "--target", jumpbox_instance_id,
        "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters",
        (
            f'host=["{remote_host}"],'
            f'portNumber=["{int(remote_port)}"],'
            f'localPortNumber=["{int(local_port)}"]'
        ),
    ]
    return _open_cmd_window(cmd, title=f"SSM DB PF {remote_host}:{remote_port}->{local_port}")


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    """
    Profiles screen.

    Backward compatibility:
    If old URL contains ?profile=xxx, redirect to the new dashboard route.
    """
    profile_name = request.args.get("profile", "").strip()
    region = request.args.get("region", "").strip()

    if profile_name:
        return redirect(url_for("dashboard", profile_name=profile_name, region=region))

    profiles = load_profiles()

    return render_template(
        "index.html",
        mode="profiles",
        profiles=profiles,
        selected_profile=None,
        available_regions=DEFAULT_REGIONS,
        selected_region="",
        update_job=None,
        app_version=APP_VERSION,
        catalog_info=None,
    )



def _copy_item_with_region(item: Dict[str, Any], region: str) -> Dict[str, Any]:
    x = dict(item or {})
    x["region"] = region
    return x


def _merge_grouped_catalogs(profile: str, regions: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    Merge existing per-region catalog JSON files into one dashboard model.
    This does not call AWS. Use Refresh Catalog with region=ALL to build missing region caches.
    """
    merged_db: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    merged_ec2: Dict[str, List[Dict[str, Any]]] = {}
    loaded_regions: List[str] = []

    for reg in regions:
        cat = _load_catalog_for(profile, reg)
        if not cat:
            continue

        loaded_regions.append(reg)

        for tech, envmap in (cat.get("db_grouped", {}) or {}).items():
            merged_db.setdefault(tech, {})
            for env, items in (envmap or {}).items():
                merged_db[tech].setdefault(env, [])
                for it in items or []:
                    merged_db[tech][env].append(_copy_item_with_region(it, reg))

        for tech, items in (cat.get("ec2_grouped", {}) or {}).items():
            merged_ec2.setdefault(tech, [])
            for it in items or []:
                merged_ec2[tech].append(_copy_item_with_region(it, reg))

    return merged_db, merged_ec2, loaded_regions


def _major_regions_for_profile(default_region: str = "") -> List[str]:
    """Return initial auto-build regions, keeping profile default first when it is one of our major regions."""
    regions: List[str] = []
    default_region = (default_region or "").strip()
    if default_region in PRIMARY_AUTO_BUILD_REGIONS:
        regions.append(default_region)
    for reg in PRIMARY_AUTO_BUILD_REGIONS:
        if reg not in regions:
            regions.append(reg)
    return regions


def _start_catalog_jobs_for_regions(profile_name: str, regions: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """Start catalog refresh jobs only where there is no cache and no job already running."""
    started: List[str] = []
    already_running: List[str] = []
    already_cached: List[str] = []

    for reg in regions:
        running_job = get_latest_job(profile_name, reg)
        if running_job and running_job.get("status") in ("starting", "running"):
            already_running.append(reg)
            continue
        if _load_catalog_for(profile_name, reg):
            already_cached.append(reg)
            continue
            purge_catalog_cache_for(profile_name, reg)
            start_update_job(profile_name, reg)
        started.append(reg)

    return started, already_running, already_cached


@app.route("/dashboard/<path:profile_name>", methods=["GET"])
def dashboard(profile_name: str):
    """
    Dedicated dashboard/instance screen for one AWS profile.
    Supports a single-region dashboard and a cached multi-region ALL dashboard.
    """
    profiles = load_profiles()

    profile_name = (profile_name or "").strip()
    region = request.args.get("region", "").strip()

    selected_profile = next((p for p in profiles if p.name == profile_name), None)
    if not selected_profile:
        flash(f"Profile not found: {profile_name}", "danger")
        return redirect(url_for("index"))

    selected_region = region or selected_profile.region or "us-east-1"
    # Show major regions first in dropdown, then all other supported regions.
    available_regions = ["ALL"] + PRIMARY_AUTO_BUILD_REGIONS + [r for r in DEFAULT_REGIONS if r not in PRIMARY_AUTO_BUILD_REGIONS]

    db_grouped = {}
    ec2_grouped = {}
    catalog_matches = False
    update_job = None

    if selected_region.upper() == "ALL":
        selected_region = "ALL"
        db_grouped, ec2_grouped, loaded_regions = _merge_grouped_catalogs(profile_name, DEFAULT_REGIONS)
        catalog_matches = bool(loaded_regions)

        if not catalog_matches:
            flash(
                f"No cached regional catalogs found for {profile_name}. "
                "Choose a single region and refresh it, or click Refresh Catalog while Region=ALL to build the 3 major regions.",
                "warning",
            )
        else:
            flash(
                f"Showing aggregated cached inventory for {profile_name}: {', '.join(loaded_regions)}",
                "info",
            )
    else:
        cat = _load_catalog_for(profile_name, selected_region)
        if cat:
            catalog_matches = True
            db_grouped = cat.get("db_grouped", {}) or {}
            ec2_grouped = cat.get("ec2_grouped", {}) or {}

            # Stamp region on records so UI actions still work if user later switches to ALL.
            for _tech, _envmap in db_grouped.items():
                for _env, _items in (_envmap or {}).items():
                    for _it in _items or []:
                        if isinstance(_it, dict):
                            _it.setdefault("region", selected_region)
            for _tech, _items in ec2_grouped.items():
                for _it in _items or []:
                    if isinstance(_it, dict):
                        _it.setdefault("region", selected_region)

        update_job = get_latest_job(profile_name, selected_region)
        if update_job and update_job.get("status") not in ("starting", "running"):
            update_job = None

        # If the selected major region already has a cache, still warm up the other
        # major regions in the background. This makes the ALL dashboard useful
        # without forcing every region in DEFAULT_REGIONS to refresh.
        if catalog_matches and selected_region in PRIMARY_AUTO_BUILD_REGIONS:
            missing_major = [r for r in _major_regions_for_profile(selected_region) if not _load_catalog_for(profile_name, r)]
            missing_major = [
                r for r in missing_major
                if not (get_latest_job(profile_name, r) and get_latest_job(profile_name, r).get("status") in ("starting", "running"))
            ]
            if missing_major:
                try:
                    auth = check_aws_profile_auth(profile_name, selected_region)
                    if auth.get("ok"):
                        for _reg in missing_major:
                            start_update_job(profile_name, _reg)
                        flash(f"Background major-region catalog warm-up started for: {', '.join(missing_major)}", "info")
                except Exception:
                    pass

        if not catalog_matches and update_job is None:
            try:
                # First-time dashboard experience:
                #   - if user lands on a major region, build the 3 major regions together
                #   - if user selects any other region, build only that selected region
                initial_regions = (
                    _major_regions_for_profile(selected_region)
                    if selected_region in PRIMARY_AUTO_BUILD_REGIONS
                    else [selected_region]
                )

                auth = check_aws_profile_auth(profile_name, selected_region)
                if not auth.get("ok"):
                    if auth.get("expired") and selected_profile.is_sso:
                        flash(
                            f"SSO session expired for {profile_name}. Go to Profiles and click SSO Login, then refresh the catalog.",
                            "warning",
                        )
                    else:
                        flash(f"AWS authentication check failed for {profile_name}:\n{auth.get('message')}", "danger")
                else:
                    started, already_running, already_cached = _start_catalog_jobs_for_regions(profile_name, initial_regions)
                    update_job = get_latest_job(profile_name, selected_region)

                    existing_cat = _load_catalog_for(profile_name, selected_region)
                    if existing_cat:
                        db_grouped = existing_cat.get("db_grouped", {}) or {}
                        ec2_grouped = existing_cat.get("ec2_grouped", {}) or {}

                    if len(initial_regions) > 1:
                        parts = []
                        if started:
                            parts.append(f"started: {', '.join(started)}")
                        if already_running:
                            parts.append(f"already running: {', '.join(already_running)}")
                        if already_cached:
                            parts.append(f"already cached: {', '.join(already_cached)}")
                        flash(
                            "Initial major-region catalog build " + ("; ".join(parts) if parts else "already completed") + ".",
                            "info",
                        )
                    else:
                        if started:
                            flash(f"Building catalog for selected region {selected_region}…", "info")
                        elif already_running:
                            flash(f"Catalog build is already running for {selected_region}…", "info")
                        elif already_cached:
                            flash(f"Loaded cached catalog for {selected_region}.", "info")

            except Exception as e:
                flash(f"Could not start catalog build for {selected_region}:\n{e}", "danger")

    return render_template(
        "index.html",
        mode="dashboard",
        profiles=profiles,
        selected_profile=selected_profile,
        available_regions=available_regions,
        selected_region=selected_region,
        db_grouped=db_grouped,
        ec2_grouped=ec2_grouped,
        update_job=update_job,
        app_version=APP_VERSION,
        catalog_info=(catalog_status(profile_name, selected_region) if selected_region != "ALL" else {
            "exists": True, "fresh": True, "status": "Aggregated",
            "age_text": "Multiple regions", "last_refresh_utc": ""
        }),
    )



def purge_catalog_cache_for(profile: str, region: str) -> None:
    ensure_tables_exist()

    for path in [catalog_json_path_for(profile, region), CATALOG_JSON_PATH]:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    con = db_conn()
    try:
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute("DELETE FROM FavoriteTarget WHERE ProfileName=? AND Region=?", (profile, region))
        con.execute("DELETE FROM Target WHERE ProfileName=? AND Region=?", (profile, region))
        con.execute("DELETE FROM UpdateJob WHERE ProfileName=? AND Region=?", (profile, region))
        con.commit()
    finally:
        con.close()

@app.route("/dashboard/<path:profile_name>/refresh-catalog", methods=["POST"])
def refresh_catalog(profile_name: str):
    profile_name = (profile_name or "").strip()
    region = request.form.get("region", "").strip() or "us-east-1"

    if not profile_name:
        flash("Missing profile for catalog refresh.", "danger")
        return redirect(url_for("index"))

    profiles = load_profiles()
    selected_profile = next((p for p in profiles if p.name == profile_name), None)

    try:
        if region.upper() == "ALL":
            auth = check_aws_profile_auth(profile_name, DEFAULT_REGIONS[0])
            if not auth.get("ok"):
                if auth.get("expired") and selected_profile and selected_profile.is_sso:
                    flash(
                        f"SSO session expired for {profile_name}. Go to Profiles and click SSO Login, then refresh again.",
                        "warning",
                    )
                else:
                    flash(f"AWS authentication check failed for {profile_name}:\n{auth.get('message')}", "danger")
                return redirect(url_for("dashboard", profile_name=profile_name, region="ALL"))

            started = []
            already = []
            for reg in PRIMARY_AUTO_BUILD_REGIONS:
                running_job = get_latest_job(profile_name, reg)
                if running_job and running_job.get("status") in ("starting", "running"):
                    already.append(reg)
                    continue
                purge_catalog_cache_for(profile_name, reg)
                start_update_job(profile_name, reg)
                started.append(reg)

            if started:
                flash(f"Major-region catalog refresh started for {len(started)} region(s): {', '.join(started)}", "info")
            if already:
                flash(f"Catalog refresh already running for: {', '.join(already)}", "info")
            if not started and not already:
                flash("No regions were queued for refresh.", "warning")

            return redirect(url_for("dashboard", profile_name=profile_name, region="ALL"))

        running_job = get_latest_job(profile_name, region)
        if running_job and running_job.get("status") in ("starting", "running"):
            flash(f"Catalog refresh is already running for {profile_name} / {region}.", "info")
        else:
            auth = check_aws_profile_auth(profile_name, region)
            if not auth.get("ok"):
                if auth.get("expired") and selected_profile and selected_profile.is_sso:
                    flash(
                        f"SSO session expired for {profile_name}. Go to Profiles and click SSO Login, then refresh again.",
                        "warning",
                    )
                else:
                    flash(f"AWS authentication check failed for {profile_name}:\n{auth.get('message')}", "danger")
            else:
                ident = auth.get("identity") or {}
                acct = ident.get("Account") or ""
                purge_catalog_cache_for(profile_name, region)
                start_update_job(profile_name, region)
                suffix = f" Account: {acct}." if acct else ""
                flash(f"Catalog refresh started for {profile_name} / {region}.{suffix}", "info")
    except Exception as e:
        flash(f"Could not start catalog refresh:\n{e}", "danger")

    return redirect(url_for("dashboard", profile_name=profile_name, region=region))


@app.route("/sync-profiles", methods=["POST"])
def sync_profiles():
    flash("Profiles refreshed from ~/.aws config/credentials.", "success")
    return redirect(url_for("index"))


@app.route("/sso-login", methods=["POST", "GET"])
def sso_login():
    try:
        profile = (
            request.form.get("profile", "").strip()
            or request.args.get("profile", "").strip()
        )

        if not profile:
            flash("Missing profile.", "danger")
            return redirect(url_for("index"))

        profiles = load_profiles()
        p = next((x for x in profiles if x.name == profile), None)
        if not p:
            flash(f"Profile not found: {profile}", "danger")
            return redirect(url_for("index"))

        cmd = aws_cmd_base() + ["sso", "login", "--profile", profile]
        code, out, err = run_cmd(cmd, timeout=900)

        if code != 0:
            msg = (err or out or "").strip() or f"AWS CLI returned exit code {code}"
            return f"""
            <html>
            <body style="font-family:Arial;padding:20px;">
              <h3>SSO login failed</h3>
              <pre>{msg}</pre>
            </body>
            </html>
            """

        default_region = p.region or "us-east-1"

        if not _load_catalog_for(profile, default_region):
            _ = start_update_job(profile, default_region)

        return redirect(url_for("dashboard", profile_name=profile, region=default_region))

    except Exception as e:
        return f"""
        <html>
        <body style="font-family:Arial;padding:20px;">
          <h3>SSO login error</h3>
          <pre>{e}</pre>
        </body>
        </html>
        """


@app.route("/update-catalog/status/<job_id>", methods=["GET"])
def update_catalog_status(job_id: str):
    ensure_tables_exist()
    con = db_conn()
    try:
        row = con.execute("SELECT * FROM UpdateJob WHERE JobId=?", (job_id,)).fetchone()
        if not row:
            return jsonify({"status": "failed"}), 404
        return jsonify(
            {
                "job_id": row["JobId"],
                "status": row["Status"],
                "last_update": row["LastUpdateUtc"],
            }
        )
    finally:
        con.close()


@app.route("/ssm-shell", methods=["POST"])
def ssm_shell():
    profile = request.form.get("profile", "").strip()
    region = request.form.get("region", "").strip() or "us-east-1"
    instance_id = request.form.get("instance_id", "").strip()

    if not profile or not instance_id:
        flash("Missing profile or instance_id for SSM Shell.", "danger")
        return redirect(request.referrer or url_for("index"))

    try:
        pid = start_ssm_shell(profile, region, instance_id)
        register_active_tunnel(
            profile=profile,
            region=region,
            tunnel_type="shell",
            target=instance_id,
            remote_host="",
            remote_port=0,
            local_port=0,
            pid=pid,
            label=f"SSM Shell / {instance_id}",
            url="",
            restart_kind="shell",
            restart_payload={
                "profile": profile,
                "region": region,
                "instance_id": instance_id,
            },
        )
        flash(f"Started SSM Shell for {instance_id}", "success")
    except Exception as e:
        flash(f"SSM Shell failed:\n{e}", "danger")

    return redirect(request.referrer or url_for("index"))


@app.route("/ssm-forward", methods=["POST"])
def ssm_forward():
    """
    DB Port Forward button handler (RDS instance / RDS cluster / DocDB cluster)
    Uses jumpbox preference (if saved) otherwise auto-detection.
    """
    profile = request.form.get("profile", "").strip()
    region = request.form.get("region", "").strip() or "us-east-1"

    target_type = request.form.get("target_type", "").strip()

    remote_host = (request.form.get("remote_host", "") or "").strip()
    remote_port = int(request.form.get("remote_port", "0") or "0")
    local_port = int(request.form.get("local_port", "0") or "0")
    env = (request.form.get("env", "") or "OTHER").strip()

    can_forward = (request.form.get("can_forward", "1") or "1").strip()
    if can_forward == "0":
        flash("DB is not available/running.", "warning")
        return redirect(request.referrer or url_for("index"))

    if not profile or not remote_host or remote_port <= 0 or local_port <= 0:
        flash(
            "Missing required inputs for DB Port Forward.\n"
            f"profile={profile} remote_host={remote_host} remote_port={remote_port} local_port={local_port}",
            "danger",
        )
        return redirect(request.referrer or url_for("index"))

    jumpbox_id = pick_jumpbox_for_target(profile, region, env, target_type)
    if not jumpbox_id:
        if (target_type or "").lower() == "docdb_cluster":
            flash(
                "No DocDB Jumpbox found for this profile/region.\n"
                "Fix: Ensure a Jumpbox exists with name containing 'docdb' and 'jumpbox' (or bastion/jump).",
                "danger",
            )
        else:
            flash(
                "No Jumpbox found for this profile/region.\n"
                "Fix: Ensure at least one EC2 instance is categorized as Jumpbox (name contains jump/bastion/ssm/etc) "
                "and that it is running + SSM managed.",
                "danger",
            )
        return redirect(request.referrer or url_for("index"))

    try:
        pid = start_ssm_port_forward_remote_host(
            profile=profile,
            region=region,
            jumpbox_instance_id=jumpbox_id,
            remote_host=remote_host,
            remote_port=remote_port,
            local_port=local_port,
        )
        register_active_tunnel(
            profile=profile,
            region=region,
            tunnel_type="db",
            target=jumpbox_id,
            remote_host=remote_host,
            remote_port=remote_port,
            local_port=local_port,
            pid=pid,
            label=remote_host,
            url="",
            restart_kind="db",
            restart_payload={
                "profile": profile,
                "region": region,
                "jumpbox_id": jumpbox_id,
                "remote_host": remote_host,
                "remote_port": remote_port,
                "local_port": local_port,
            },
        )
        flash(
            f"Starting DB tunnel via Jumpbox {jumpbox_id} -> {remote_host}:{remote_port} on localhost:{local_port}",
            "success",
        )
    except Exception as e:
        flash(f"DB port forward failed:\n{e}", "danger")

    return redirect(request.referrer or url_for("index"))


@app.route("/webui-forward", methods=["POST"])
def webui_forward():
    profile = request.form.get("profile", "").strip()
    region = request.form.get("region", "").strip() or "us-east-1"
    instance_id = request.form.get("instance_id", "").strip()
    local_port = int(request.form.get("local_port", "0") or "0")
    remote_port = int(request.form.get("remote_port", "8080") or "8080")
    can_forward = (request.form.get("can_forward", "1") or "1").strip()

    if can_forward == "0":
        flash("Instance is not running.", "warning")
        return redirect(request.referrer or url_for("index"))

    if not profile or not instance_id or local_port <= 0:
        flash("Missing required inputs for Web UI Port Forward.", "danger")
        return redirect(request.referrer or url_for("index"))

    try:
        web_type = (request.form.get("web_type") or "webui").strip()
        web_type_l = web_type.lower()
        open_path = "/browser/" if "neo4j" in web_type_l else "/"

        pid = start_ssm_port_forward_ec2(
            profile=profile,
            region=region,
            instance_id=instance_id,
            remote_port=remote_port,
            local_port=local_port,
            auto_open=True,
            open_path=open_path,
        )
        register_active_tunnel(
            profile=profile,
            region=region,
            tunnel_type=web_type,
            target=instance_id,
            remote_host="127.0.0.1",
            remote_port=remote_port,
            local_port=local_port,
            pid=pid,
            label=f"{web_type} / {instance_id}",
            url=f"http://127.0.0.1:{local_port}{open_path}",
            restart_kind="webui",
            restart_payload={
                "profile": profile,
                "region": region,
                "instance_id": instance_id,
                "remote_port": remote_port,
                "local_port": local_port,
                "auto_open": True,
                "open_path": open_path,
            },
        )
        flash(f"Starting Web UI tunnel remote port {remote_port} on localhost:{local_port} (opening browser soon)...", "success")
    except Exception as e:
        flash(f"Web UI port forward failed:\n{e}", "danger")

    return redirect(request.referrer or url_for("index"))


def run_flask():
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5050"))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run_flask()