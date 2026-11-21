"""Shared radio catalog (in parity with Tradewind Commander) + driver mapping.

Orca supports the same radios as Tradewind Commander by loading the same preset
list (orca/data/radios.json, ported from the Commander's PRESETS). Each preset
is mapped to the Orca subsystem that owns it and to the Windows driver it needs,
so the catalog is the single source of truth for "what can I plug in".
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

from . import drivers


def _data_path() -> Path:
    # Works in a normal install and inside a PyInstaller bundle (_MEIPASS).
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "orca" / "data" / "radios.json"
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "data" / "radios.json"


_DATA = _data_path()


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return json.loads(_DATA.read_text(encoding="utf-8"))
    except Exception:
        return {"source": "", "radios": []}


def catalog() -> list[dict]:
    """The full preset list, as shared with Tradewind Commander."""
    return list(_load().get("radios", []))


def source() -> str:
    return _load().get("source", "")


def get(radio_id: str) -> dict | None:
    return next((r for r in catalog() if r.get("id") == radio_id), None)


def subsystem_of(preset: dict) -> dict:
    """Map a preset's interface to the Orca path that reads it."""
    tnc = (preset.get("tnc") or "none").lower()
    ptt = (preset.get("ptt") or "").upper()
    label = (preset.get("label") or "").lower()
    if "sdr" in label or "rtlsdr" in preset.get("id", ""):
        return {"subsystem": "SDR front-end", "bind": "sdr",
                "how": "receive-only IQ via SoapySDR"}
    if tnc in ("kiss_serial", "kiss_tcp"):
        return {"subsystem": "Packet / KISS TNC", "bind": "aprs",
                "how": "KISS frames -> Tradewind APRS + mesh transport (2 m/70 cm)"}
    if tnc == "sound":
        # a soundcard rig with CI-V/CAT is both a decode source and a controllable rig
        if ptt in ("CIV", "YAESU_CAT", "RTS", "DTR", "RIGCTLD"):
            return {"subsystem": "CAT transceiver + audio", "bind": "uranus",
                    "how": "CAT/CI-V rig control + USB audio -> WSJT-X/Uranus (FT8/WEFAX) on UDP 2237"}
        return {"subsystem": "Audio interface (VOX)", "bind": "uranus",
                "how": "USB audio -> WSJT-X/Uranus; PTT by VOX"}
    return {"subsystem": "Software / network link", "bind": "aprs",
            "how": "Hamlib rigctld, APRS-IS, or a TCP/BT sound modem"}


def entry(preset: dict) -> dict:
    """Full view of one radio: identity + subsystem + driver plan."""
    e = {"id": preset.get("id"), "label": preset.get("label"),
         "tnc": preset.get("tnc"), "ptt": preset.get("ptt"),
         "note": preset.get("note", "")}
    e.update(subsystem_of(preset))
    e["driver"] = drivers.plan_dict(preset)
    e["plug_and_play"] = e["driver"]["plug_and_play"]
    return e


def all_entries() -> list[dict]:
    return [entry(r) for r in catalog()]


def report() -> str:
    """Human-readable catalog for `orca --list-radios`."""
    rows = all_entries()
    lines = [f"Orca radio catalog  -  {len(rows)} radios (shared with Tradewind Commander)",
             "=" * 66]
    for e in rows:
        pnp = "plug-and-play" if e["plug_and_play"] else "driver step needed"
        lines.append(f"\n{e['id']:<16} {e['label']}")
        lines.append(f"    -> {e['subsystem']}  ({pnp})")
        drv = ", ".join(d["interface"] for d in e["driver"]["drivers"])
        lines.append(f"    driver: {drv}")
    lines.append(f"\nSource: {source()}")
    return "\n".join(lines)
