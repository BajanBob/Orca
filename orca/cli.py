"""Orca server command line."""
import argparse
import sys
from pathlib import Path

import uvicorn

from . import config
from .server import create_app


def main():
    ap = argparse.ArgumentParser(prog="orca")
    ap.add_argument("--config", help="site YAML (defaults to the simulator)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    path = a.config
    if not path:   # no --config: use ./site.yaml, else the bundled example (simulator)
        here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        for c in (Path.cwd() / "site.yaml", here / "config" / "site.example.yaml"):
            if c.exists():
                path = c
                break
    cfg = config.load(path)
    print(f"Config: {path or 'built-in defaults'}")
    host = a.host or cfg["server"]["host"]
    port = a.port or cfg["server"]["port"]
    print(f"Orca console on http://{host}:{port}  (sdr: {cfg['sdr']['kind']})")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
