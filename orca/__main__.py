"""Run the Orca server:  python -m orca --config config/site.yaml"""
import argparse

import uvicorn

from . import config
from .server import create_app


def main():
    ap = argparse.ArgumentParser(prog="orca")
    ap.add_argument("--config", help="site YAML (defaults to the simulator)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    cfg = config.load(a.config)
    host = a.host or cfg["server"]["host"]
    port = a.port or cfg["server"]["port"]
    print(f"Orca console on http://{host}:{port}  (sdr: {cfg['sdr']['kind']})")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
