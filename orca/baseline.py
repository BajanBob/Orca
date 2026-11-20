"""Per-site spectrum baseline: learns normal occupancy by band, bin and hour of week."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import numpy as np

BINS = 256


def hour_of_week(ts: float) -> int:
    t = time.gmtime(ts)
    return t.tm_wday * 24 + t.tm_hour


def coarse(db: np.ndarray, bins: int = BINS) -> np.ndarray:
    idx = np.linspace(0, len(db), bins + 1).astype(int)
    return np.maximum.reduceat(db, idx[:-1])


class Baseline:
    """EWMA mean and variance per (view, hour-of-week, bin).

    `score` returns a z-score per bin; positive means louder than normal for
    this site at this hour. Scores are 0 until `min_samples` frames are learnt.
    """

    def __init__(self, path: str | Path | None, alpha: float = 0.02, min_samples: int = 30,
                 hourly: bool = True):
        self.alpha, self.min_samples, self.hourly = alpha, min_samples, hourly
        self.state: dict[tuple[str, int], list] = {}   # key -> [n, mean, var]
        self.db = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(path), check_same_thread=False)
            self.db.execute("create table if not exists baseline(view text, how int, n int,"
                            " mean blob, var blob, primary key(view, how))")
            for view, how, n, m, v in self.db.execute("select * from baseline"):
                self.state[(view, how)] = [n, np.frombuffer(m, np.float32).copy(),
                                           np.frombuffer(v, np.float32).copy()]

    def _key(self, view: str, ts: float) -> tuple[str, int]:
        return view, (hour_of_week(ts) if self.hourly else 0)

    def update(self, view: str, db: np.ndarray, ts: float | None = None) -> None:
        x = coarse(db).astype(np.float32)
        k = self._key(view, ts or time.time())
        s = self.state.get(k)
        if s is None:
            self.state[k] = [1, x.copy(), np.full_like(x, 4.0)]
            return
        n, m, v = s
        a = max(self.alpha, 1.0 / (n + 1))       # fast start, then slow EWMA
        d = x - m
        m += a * d
        v[:] = (1 - a) * (v + a * d * d)
        s[0] = n + 1

    def score(self, view: str, db: np.ndarray, ts: float | None = None) -> np.ndarray:
        x = coarse(db).astype(np.float32)
        s = self.state.get(self._key(view, ts or time.time()))
        if s is None or s[0] < self.min_samples:
            return np.zeros_like(x)
        _, m, v = s
        return (x - m) / np.sqrt(np.maximum(v, 1.0))

    def samples(self, view: str, ts: float | None = None) -> int:
        s = self.state.get(self._key(view, ts or time.time()))
        return s[0] if s else 0

    def save(self) -> None:
        if not self.db:
            return
        with self.db:
            for (view, how), (n, m, v) in self.state.items():
                self.db.execute("insert or replace into baseline values(?,?,?,?,?)",
                                (view, how, n, m.astype(np.float32).tobytes(),
                                 v.astype(np.float32).tobytes()))
