"""Orca Desktop: runs the Orca server and opens the console in its own window.

Used for the Windows desktop build. On first run it writes an editable
site.yaml (simulator by default) next to the app's data folder.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import config
from .server import create_app


def _resource(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def home_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Orca"
    root.mkdir(parents=True, exist_ok=True)
    return root


def site_config() -> Path:
    cfg = home_dir() / "site.yaml"
    if not cfg.exists():
        example = _resource("config/site.example.yaml")
        text = example.read_text(encoding="utf-8") if example.exists() else ""
        text = text.replace("data_dir: ./data", f"data_dir: {(home_dir() / 'data').as_posix()}")
        cfg.write_text(text, encoding="utf-8")
    return cfg


def free_port(preferred: int) -> int:
    for p in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def ensure_streams() -> None:
    """A windowed (no-console) build has sys.stdout/sys.stderr set to None.
    Libraries such as uvicorn call .isatty() on them and crash, so send that
    output to a log file in the app's data folder instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = open(home_dir() / "orca-desktop.log", "a", buffering=1, encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main():
    ensure_streams()
    cfg_path = site_config()
    cfg = config.load(cfg_path)
    host = os.environ.get("ORCA_HOST", "127.0.0.1")   # set 0.0.0.0 to share with tablets/phones
    port = free_port(int(os.environ.get("ORCA_PORT", cfg["server"]["port"])))
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host=host, port=port,
                                          log_level="warning", log_config=None))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(100):                      # wait until the server answers
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    print(f"Orca running at {url}  (config: {cfg_path})")
    try:
        import webview  # pywebview
        webview.create_window("Orca", url, width=1440, height=920, min_size=(900, 640))
        webview.start()
    except Exception:
        webbrowser.open(url)                  # no embedded browser available: use the default one
        try:
            while t.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    server.should_exit = True


if __name__ == "__main__":
    if "--copy-config" in sys.argv:
        shutil.copy(site_config(), Path.cwd() / "site.yaml")
    main()
