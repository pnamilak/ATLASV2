# launcher_desktop.py
import os
import sys
import time
import socket
import threading
import traceback
import webbrowser
import logging
from pathlib import Path

APP_TITLE = "AWS Trusted Login & Access Service"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5050

# --------------------------------------------------------------------------------------
# Ultra-safe file logging (never fail silently)
# --------------------------------------------------------------------------------------

def _safe_get_localappdata() -> str:
    # Windows-only helper used by the Windows desktop/installer path.
    v = os.environ.get("LOCALAPPDATA", "")
    if v:
        return v
    return str(Path.home() / "AppData" / "Local")


def data_dir() -> Path:
    # Keep application data native per platform.
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ATLAS"
    if os.name == "nt":
        return Path(_safe_get_localappdata()) / "ATLAS"
    return Path.home() / ".local" / "share" / "ATLAS"


def log_dir() -> Path:
    return data_dir() / "logs"


def launcher_log_path_primary() -> Path:
    return log_dir() / "launcher.log"


def launcher_log_path_fallback() -> Path:
    # if LOCALAPPDATA write fails for any reason, log here
    return Path(os.environ.get("TEMP", str(Path.home()))) / "ATLAS_launcher_fallback.log"


def _write_fallback(text: str) -> None:
    try:
        p = launcher_log_path_fallback()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", errors="ignore") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception:
        # absolutely last resort: do nothing
        pass


def setup_logger() -> logging.Logger:
    """
    Never raise from here. If primary log path fails, use TEMP fallback.
    """
    logger = logging.getLogger("ATLAS.Launcher")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    # Try primary
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(launcher_log_path_primary()), encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Logger initialized (primary): %s", str(launcher_log_path_primary()))
        return logger
    except Exception as ex:
        _write_fallback("LOGGER_PRIMARY_FAILED: " + repr(ex))

    # Fallback
    try:
        fh = logging.FileHandler(str(launcher_log_path_fallback()), encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Logger initialized (fallback): %s", str(launcher_log_path_fallback()))
        return logger
    except Exception as ex:
        _write_fallback("LOGGER_FALLBACK_FAILED: " + repr(ex))

    return logger


LOG = setup_logger()

# pywebview is optional at runtime; fallback to system browser if missing
try:
    import webview  # type: ignore
except Exception as ex:
    webview = None
    try:
        LOG.info("pywebview import failed: %r", ex)
    except Exception:
        _write_fallback("pywebview import failed: " + repr(ex))


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_base_dir() -> Path:
    """
    In one-dir builds, sys._MEIPASS points to the _internal folder in dist.
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent


def ensure_import_paths() -> None:
    base = app_base_dir()

    # Desktop GUI apps on macOS do not inherit the interactive shell PATH.
    # Add the standard AWS CLI / Session Manager Plugin locations explicitly.
    if sys.platform == "darwin":
        current = [x for x in (os.environ.get("PATH") or "").split(os.pathsep) if x]
        for item in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/local/sessionmanagerplugin/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
            if item not in current:
                current.insert(0, item)
        os.environ["PATH"] = os.pathsep.join(current)
        LOG.info("macOS normalized PATH=%s", os.environ.get("PATH", ""))

    # Ensure we run from _internal so templates/static are resolvable
    os.chdir(str(base))
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    LOG.info("BaseDir=%s", str(base))
    LOG.info("CWD=%s", os.getcwd())
    LOG.info("Frozen=%s", str(is_frozen()))
    LOG.info("sys.executable=%s", sys.executable)
    if hasattr(sys, "_MEIPASS"):
        LOG.info("sys._MEIPASS=%s", str(getattr(sys, "_MEIPASS")))


def has_webview2_runtime() -> bool:
    if os.name != "nt":
        return True

    candidates = [
        r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
        r"C:\Program Files\Microsoft\EdgeWebView\Application",
    ]
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        candidates.append(os.path.join(la, r"Microsoft\EdgeWebView\Application"))

    for p in candidates:
        if os.path.isdir(p):
            return True

    try:
        import winreg  # type: ignore
        reg_candidates = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        ]
        for root, key in reg_candidates:
            try:
                winreg.OpenKey(root, key)
                return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def port_is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False


def can_bind(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def pick_port(host: str, preferred: int, span: int = 50) -> int:
    if can_bind(host, preferred):
        return preferred
    for p in range(preferred + 1, preferred + span + 1):
        if can_bind(host, p):
            return p
    return preferred


def wait_for_port(host: str, port: int, timeout_sec: int = 40) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if port_is_listening(host, port):
            return True
        time.sleep(0.35)
    return False


def show_friendly_error(msg: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, msg)
        root.destroy()
    except Exception:
        # as last resort, write to fallback log
        _write_fallback("FRIENDLY_ERROR_FAILED\n" + msg)


def _serve_using_main(host: str, port: int) -> None:
    os.environ["DASHBOARD_HOST"] = host
    os.environ["DASHBOARD_PORT"] = str(port)

    LOG.info("Server: importing main (cwd=%s)", os.getcwd())

    import main  # noqa

    # If your main.py supports serve_app(host,port), use it.
    if hasattr(main, "serve_app"):
        LOG.info("Server: calling main.serve_app()")
        main.serve_app()
        return

    # Fallback: if it exposes app as 'app' or 'APP', use waitress
    flask_app = None
    if hasattr(main, "app"):
        flask_app = getattr(main, "app")
    elif hasattr(main, "APP"):
        flask_app = getattr(main, "APP")

    if flask_app is not None:
        from waitress import serve  # type: ignore
        LOG.info("Server: waitress.serve(flask_app) host=%s port=%d", host, port)
        serve(flask_app, host=host, port=port, threads=8)
        return

    raise RuntimeError("main.py does not expose serve_app(), app, or APP")


def start_server_thread(host: str, port: int) -> None:
    try:
        _serve_using_main(host, port)
        LOG.info("Server returned (unexpected).")
    except Exception as ex:
        LOG.error("SERVER_CRASH: %r", ex)
        LOG.error(traceback.format_exc())
        _write_fallback("SERVER_CRASH: " + repr(ex) + "\n" + traceback.format_exc())


def cleanup_atlas_sessions(reason: str = "launcher-exit") -> None:
    """Best-effort cleanup of SSM child processes tracked by main.py."""
    try:
        import main  # noqa
        if hasattr(main, "stop_all_active_tunnels"):
            result = main.stop_all_active_tunnels()
            LOG.info("Session cleanup (%s): stopped=%s count=%s ok=%s",
                     reason, result.get("stopped"), result.get("count"), result.get("ok"))
    except Exception as ex:
        LOG.error("Session cleanup failed (%s): %r", reason, ex)


def run_browser_mode(url: str, server_thread: threading.Thread) -> None:
    LOG.info("Browser mode: opening system browser: %s", url)
    try:
        webbrowser.open(url)
    except Exception as ex:
        LOG.error("webbrowser.open failed: %r", ex)

    # keep process alive a bit so user sees something
    try:
        while server_thread.is_alive():
            time.sleep(1.0)
    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt received; exiting.")
    finally:
        cleanup_atlas_sessions("browser-mode-exit")


def main_entry() -> None:
    ensure_import_paths()

    host = os.environ.get("DASHBOARD_HOST", DEFAULT_HOST)
    preferred_port = int(os.environ.get("DASHBOARD_PORT", str(DEFAULT_PORT)))

    chosen_port = pick_port(host, preferred_port)
    url = f"http://{host}:{chosen_port}/"

    LOG.info("Launcher starting host=%s preferred_port=%d chosen_port=%d url=%s",
             host, preferred_port, chosen_port, url)
    LOG.info("webview_available=%s webview2_probe=%s",
             str(webview is not None), str(has_webview2_runtime()))
    LOG.info("primary_log=%s", str(launcher_log_path_primary()))
    LOG.info("fallback_log=%s", str(launcher_log_path_fallback()))

    server_thread = threading.Thread(target=start_server_thread, args=(host, chosen_port), daemon=True)
    server_thread.start()

    ok = wait_for_port(host, chosen_port, timeout_sec=40)
    LOG.info("Port listening=%s host=%s port=%d", str(ok), host, chosen_port)

    if not ok:
        msg = (
            "The app UI could not start because the local server did not come online.\n\n"
            f"URL attempted:\n  {url}\n\n"
            "Please check logs:\n"
            f"  Primary:  {str(launcher_log_path_primary())}\n"
            f"  Fallback: {str(launcher_log_path_fallback())}\n"
        )
        show_friendly_error(msg)
        run_browser_mode(url, server_thread)
        return

    # If pywebview is missing, fallback to browser
    if webview is None:
        LOG.info("pywebview missing -> browser mode.")
        run_browser_mode(url, server_thread)
        return

    try:
        LOG.info("Opening pywebview window.")
        class AtlasApi:
            def open_new_window(self, target_url: str):
                try:
                    if not target_url:
                        return False

                    if target_url.startswith("/"):
                        target_url = url.rstrip("/") + target_url

                    webview.create_window(
                        APP_TITLE,
                        target_url,
                        width=1280,
                        height=800,
                        text_select=True,
                        confirm_close=True
                    )
                    return True
                except Exception as ex:
                    LOG.error("open_new_window failed: %r", ex)
                    return False


        api = AtlasApi()

        main_window = webview.create_window(
            APP_TITLE,
            url,
            width=1280,
            height=800,
            text_select=True,
            confirm_close=True,
            js_api=api
        )

        def intercept_blank_links():
            js = """
            document.addEventListener('click', function(e) {
            const a = e.target.closest('a[target="_blank"]');
            if (!a) return;

            e.preventDefault();

            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.open_new_window(a.getAttribute('href'));
            } else {
                window.location.href = a.href;
            }
            }, true);
            """
            try:
                main_window.evaluate_js(js)
            except Exception as ex:
                LOG.error("intercept_blank_links failed: %r", ex)

        main_window.events.loaded += intercept_blank_links

        if sys.platform == "darwin":
            LOG.info("Starting pywebview with native macOS renderer.")
            webview.start()
        elif os.name == "nt":
            LOG.info("Starting pywebview with Edge Chromium renderer.")
            webview.start(gui="edgechromium")
        else:
            LOG.info("Starting pywebview with platform-default renderer.")
            webview.start()
        cleanup_atlas_sessions("desktop-window-closed")
    except Exception as ex:
        LOG.error("pywebview failed. Falling back to browser: %r", ex)
        LOG.error(traceback.format_exc())
        run_browser_mode(url, server_thread)


if __name__ == "__main__":
    try:
        main_entry()
    except Exception as ex:
        # Absolute top-level crash handler
        txt = "LAUNCHER_FATAL: " + repr(ex) + "\n" + traceback.format_exc()
        _write_fallback(txt)
        try:
            LOG.error(txt)
        except Exception:
            pass
        show_friendly_error(
            "Launcher crashed.\n\n"
            "Please check the fallback log:\n"
            f"{str(launcher_log_path_fallback())}\n"
        )