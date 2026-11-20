"""Configuration and region packs.

Region packs hold defaults that differ by country or ITU region. They are
starting points: always check your national regulator's current rules.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

# Channels protected worldwide by ITU Radio Regulations (distress, safety, GNSS).
GLOBAL_PROTECTED = [
    {"name": "MF distress 2182 kHz", "lo": 2181.5e3, "hi": 2182.5e3, "safety": True},
    {"name": "Aero emergency 121.5 MHz", "lo": 121.49e6, "hi": 121.51e6, "safety": True},
    {"name": "Marine ch 16 (156.800 MHz)", "lo": 156.79375e6, "hi": 156.80625e6, "safety": True},
    {"name": "Marine DSC ch 70 (156.525 MHz)", "lo": 156.51875e6, "hi": 156.53125e6, "safety": True},
    {"name": "COSPAS-SARSAT 406 MHz", "lo": 406.0e6, "hi": 406.1e6, "safety": True},
    {"name": "GPS L1 (1575.42 MHz)", "lo": 1574.4e6, "hi": 1576.44e6, "safety": True},
]

REGION_PACKS = {
    "itu1": {"name": "ITU Region 1: Europe, Africa, Middle East, Russia",
             "aprs_hz": 144.800e6, "ism_lora_hz": 868.1e6},
    "itu2": {"name": "ITU Region 2: the Americas and the Caribbean",
             "aprs_hz": 144.390e6, "ism_lora_hz": 915.0e6},
    "itu3": {"name": "ITU Region 3: Asia-Pacific",
             "aprs_hz": None, "ism_lora_hz": None,
             "note": "APRS and LoRa frequencies vary by country in Region 3; set them per site."},
}

DEFAULTS = {
    "site": {"name": "Orca site", "node": "ORCA-01", "region": "itu2", "lat": 0.0, "lon": 0.0},
    "data_dir": "./data",
    "sdr": {"kind": "sim", "scenarios": []},
    "inventory": {"receivers": ["sim"], "frontends": []},
    "view": {"lo": 1568e6, "hi": 1583e6},
    "display_bins": 1024,
    "nodes": [],
    "aprs": {"mode": "sim", "callsign": "", "transmit": False, "watch": []},
    "uranus": {"mode": "sim", "port": 2237},
    "syslog": {"mode": "sim", "port": 5514},
    "protected_extra": [],
    "anomaly_z": 6.0,
    "agent": {"autorun_safe_steps": True, "llm": {"enabled": False, "model": "claude-sonnet-5-5"}},
    "server": {"host": "0.0.0.0", "port": 8088},
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class _Loader(yaml.SafeLoader):
    """SafeLoader that also reads 1565.0e6 / 20e6 as floats (YAML 1.1 wants e+6)."""


_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$|^[-+]?\.(?:inf|Inf|INF)$|^\.(?:nan|NaN|NAN)$"),
    list("-+0123456789."))


def load(path: str | Path | None) -> dict:
    cfg = DEFAULTS
    if path:
        cfg = _merge(DEFAULTS, yaml.load(Path(path).read_text(), Loader=_Loader) or {})
    cfg = copy.deepcopy(cfg)
    cfg["region_pack"] = REGION_PACKS.get(cfg["site"]["region"], REGION_PACKS["itu2"])
    cfg["protected"] = GLOBAL_PROTECTED + cfg.get("protected_extra", [])
    return cfg
