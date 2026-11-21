"""Emitter fingerprinting: recognise a transmitter again from how its signal looks.

A fingerprint is the feature vector plus the measured centre, bandwidth and (when
known) modulation. New detections are matched to the library by nearest neighbour
in a whitened feature space; a close match is the same emitter, a far one is new
and gets added. This is what lets Orca say "this is the jammer from Tuesday".
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import numpy as np

from .features import extract


class EmitterLibrary:
    def __init__(self, path: str | None = None, match_dist: float = 2.5):
        self.path = Path(path) if path else None
        self.match_dist = match_dist
        self.emitters: list[dict] = []
        self._scale = None
        if self.path and self.path.exists():
            self.emitters = json.loads(self.path.read_text(encoding="utf-8"))
            self._refit()

    def _refit(self):
        if len(self.emitters) >= 3:
            F = np.array([e["feat"] for e in self.emitters])
            self._scale = F.std(0) + 1e-6
        else:
            self._scale = None

    def _dist(self, a, b):
        s = self._scale if self._scale is not None else np.ones(len(a))
        return float(np.sqrt(np.mean(((np.array(a) - np.array(b)) / s) ** 2)))

    def observe(self, iq, rate: float, center: float, bw: float,
                modulation: str = "", node: str = "") -> dict:
        feat = extract(iq, rate).tolist()
        best, bd = None, 1e9
        for e in self.emitters:
            # only compare emitters near the same frequency/bandwidth
            if abs(e["center"] - center) > max(bw, e["bw"]):
                continue
            # a different, known modulation at the same spot is a different emitter
            if modulation and e["modulation"] and e["modulation"] != modulation:
                continue
            d = self._dist(feat, e["feat"])
            if d < bd:
                best, bd = e, d
        now = time.time()
        if best and bd <= self.match_dist:
            best["count"] += 1
            best["last_seen"] = now
            best["feat"] = (0.9 * np.array(best["feat"]) + 0.1 * np.array(feat)).tolist()
            if node and node not in best["nodes"]:
                best["nodes"].append(node)
            self._save()
            return {"match": True, "id": best["id"], "distance": round(bd, 2),
                    "label": best["label"], "count": best["count"]}
        e = {"id": "emt_" + uuid.uuid4().hex[:8], "center": center, "bw": bw,
             "modulation": modulation, "label": modulation or "unidentified",
             "feat": feat, "first_seen": now, "last_seen": now, "count": 1,
             "nodes": [node] if node else []}
        self.emitters.append(e)
        self._refit()
        self._save()
        return {"match": False, "id": e["id"], "distance": round(bd, 2) if best else None,
                "label": e["label"], "count": 1}

    def label(self, emitter_id: str, label: str) -> bool:
        for e in self.emitters:
            if e["id"] == emitter_id:
                e["label"] = label
                self._save()
                return True
        return False

    def list(self) -> list[dict]:
        return [{k: e[k] for k in ("id", "center", "bw", "modulation", "label",
                                   "count", "first_seen", "last_seen", "nodes")}
                for e in sorted(self.emitters, key=lambda x: -x["last_seen"])]

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.emitters), encoding="utf-8")
