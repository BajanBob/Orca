"""Data and screen capture: IQ recordings as SigMF, spectrum snapshots, console screenshots."""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import __version__


def _stamp(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class CaptureStore:
    def __init__(self, root: str | Path, node: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.node = node

    def _safe(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError("bad name")
        return self.root / name

    def save_iq(self, iq: np.ndarray, fc: float, rate: float, ts: float,
                annotations: list[dict] | None = None, note: str = "", hw: str = "") -> str:
        base = f"orca_{self.node}_{_stamp(ts)}_{fc / 1e6:.3f}MHz"
        iq.astype(np.complex64).tofile(self.root / f"{base}.sigmf-data")
        meta = {
            "global": {"core:datatype": "cf32_le", "core:sample_rate": float(rate),
                       "core:version": "1.0.0", "core:hw": hw, "core:author": f"Orca node {self.node}",
                       "core:recorder": f"Orca {__version__}", "core:description": note},
            "captures": [{"core:sample_start": 0, "core:frequency": float(fc), "core:datetime": _iso(ts)}],
            "annotations": [
                {"core:sample_start": 0, "core:sample_count": int(len(iq)),
                 "core:freq_lower_edge": a["f_lo"], "core:freq_upper_edge": a["f_hi"],
                 "core:label": f"emission {a['bw_occ'] / 1e3:.1f} kHz occ, {a['snr_db']:.1f} dB SNR"}
                for a in (annotations or [])],
        }
        (self.root / f"{base}.sigmf-meta").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return base

    def save_spectrum(self, frame_msg: dict) -> str:
        base = f"orca_{self.node}_{_stamp(frame_msg['ts'])}_spectrum"
        db = frame_msg["db"]
        n = len(db)
        lines = ["freq_hz,power_db"] + [
            f"{frame_msg['lo'] + (frame_msg['hi'] - frame_msg['lo']) * (i + .5) / n:.0f},{v}" for i, v in enumerate(db)]
        (self.root / f"{base}.csv").write_text("\n".join(lines), encoding="utf-8")
        return base

    def save_screen(self, data_url: str, meta: dict) -> str:
        m = re.match(r"data:image/(png|jpeg);base64,(.+)", data_url, re.S)
        if not m:
            raise ValueError("expected a PNG or JPEG data URL")
        raw = base64.b64decode(m.group(2))
        if len(raw) > 20_000_000:
            raise ValueError("image too large")
        base = f"orca_{self.node}_{_stamp()}_screen"
        ext = "png" if m.group(1) == "png" else "jpg"
        (self.root / f"{base}.{ext}").write_bytes(raw)
        (self.root / f"{base}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return f"{base}.{ext}"

    def list(self) -> list[dict]:
        files = sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
                for p in files if p.is_file()][:200]

    def path(self, name: str) -> Path:
        p = self._safe(name)
        if not p.exists():
            raise FileNotFoundError(name)
        return p
