"""Spectrum engine: PSD, emission detection and bandwidth sampling, wideband sweeps."""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .bands import plan as make_plan


def psd(iq: np.ndarray, rate: float, nfft: int = 2048) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD, 50 % overlap, Hann window. Returns (offset_hz, dB)."""
    nfft = min(nfft, max(64, 1 << (len(iq).bit_length() - 1)))
    win = np.hanning(nfft).astype(np.float32)
    step = nfft // 2
    nseg = max(1, (len(iq) - nfft) // step + 1)
    acc = np.zeros(nfft)
    for i in range(nseg):
        seg = iq[i * step:i * step + nfft]
        if len(seg) < nfft:
            break
        acc += np.abs(np.fft.fft(seg * win)) ** 2
    acc /= nseg * (win ** 2).sum()
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rate))
    return freqs, 10 * np.log10(np.fft.fftshift(acc) + 1e-20)


def rebin_max(db: np.ndarray, n: int) -> np.ndarray:
    """Down-sample to n display bins keeping peaks (max-hold per bin)."""
    if len(db) == n:
        return db
    if len(db) < n:
        return np.interp(np.linspace(0, len(db) - 1, n), np.arange(len(db)), db)
    idx = np.linspace(0, len(db), n + 1).astype(int)
    return np.maximum.reduceat(db, idx[:-1])


@dataclass
class Emission:
    f_center: float
    f_lo: float
    f_hi: float
    bw_occ: float        # 99 % occupied bandwidth (Hz)
    bw_3db: float        # -3 dB bandwidth (Hz)
    peak_db: float
    snr_db: float

    def to_dict(self):
        return {k: float(v) for k, v in self.__dict__.items()}


def noise_floor(db: np.ndarray) -> float:
    """Robust floor: median of the lower 60 % of bins."""
    s = np.sort(db)
    return float(np.median(s[: max(1, int(len(s) * 0.6))]))


def detect(freqs: np.ndarray, db: np.ndarray, thresh_db: float = 8.0,
           min_bins: int = 1, occ: float = 0.99) -> list[Emission]:
    """Find contiguous regions above floor + thresh; measure each one."""
    floor = noise_floor(db)
    above = db > floor + thresh_db
    out: list[Emission] = []
    if not above.any():
        return out
    d = np.diff(np.concatenate(([0], above.astype(int), [0])))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    binw = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 1.0
    floor_lin = 10 ** (floor / 10)
    for a, b in zip(starts, ends):
        if b - a < min_bins:
            continue
        # widen the region to where the signal meets the floor, so the 99 % calc sees skirts
        a2, b2 = a, b
        while a2 > 0 and db[a2 - 1] > floor + 3:
            a2 -= 1
        while b2 < len(db) and db[b2] > floor + 3:
            b2 += 1
        seg = db[a2:b2]
        lin = np.maximum(10 ** (seg / 10) - floor_lin, 0)
        tot = lin.sum()
        pk = int(np.argmax(seg))
        peak = float(seg[pk])
        if tot > 0:
            c = np.cumsum(lin) / tot
            lo_i = int(np.searchsorted(c, (1 - occ) / 2))
            hi_i = int(np.searchsorted(c, 1 - (1 - occ) / 2))
            bw_occ = max(1, hi_i - lo_i + 1) * binw
            centroid = float((freqs[a2:b2] * lin).sum() / tot)
        else:
            bw_occ, centroid = binw, float(freqs[a2 + pk])
        m3 = seg >= peak - 3
        bw3 = max(1, int(m3.sum())) * binw
        out.append(Emission(centroid, float(freqs[a2]), float(freqs[b2 - 1]) + binw,
                            bw_occ, bw3, peak, peak - floor))
    return out


@dataclass
class Frame:
    lo: float
    hi: float
    db: np.ndarray
    floor: float
    emissions: list[Emission]
    ts: float
    mode: str            # "iq" (single tune) or "sweep"
    rbw: float

    def to_msg(self) -> dict:
        return {"type": "frame", "lo": self.lo, "hi": self.hi, "ts": self.ts, "mode": self.mode,
                "rbw": self.rbw, "floor": round(self.floor, 1),
                "db": np.round(self.db, 1).tolist(),
                "emissions": [e.to_dict() for e in self.emissions[:40]]}


class Spectrum:
    """Produces display frames for any span, single-tune or swept."""

    def __init__(self, source, receivers: list[str], frontends: list[str],
                 nbins: int = 1024, nfft: int = 2048):
        self.src, self.receivers, self.frontends = source, receivers, frontends
        self.nbins, self.nfft = nbins, nfft
        self.last_iq: tuple[float, float, np.ndarray] | None = None   # (fc, rate, iq)

    async def frame(self, lo: float, hi: float) -> Frame:
        span = hi - lo
        p = make_plan(lo, hi, self.receivers, self.frontends)
        if not p.segments:
            return Frame(lo, hi, np.full(self.nbins, -140.0), -140.0, [], time.time(), "gap", 0)
        if len(p.segments) == 1:
            s = p.segments[0]
            rate = min(s.rate, max(span * 1.25, 250e3))   # narrow spans get finer resolution
            fc = (lo + hi) / 2
            iq = await self.src.capture(fc, rate, self.nfft * 8, s.frontend)
            self.last_iq = (fc, rate, iq)
            f, db = psd(iq, rate, self.nfft)
            f = f + fc
            keep = (f >= lo) & (f <= hi)
            f, db = f[keep], db[keep]
            ems = detect(f, db)
            return Frame(lo, hi, rebin_max(db, self.nbins), noise_floor(db), ems,
                         time.time(), "iq", rate / self.nfft)
        if hasattr(self.src, "span_psd"):           # simulator fast path
            db = self.src.span_psd(lo, hi, self.nbins * 4)
            f = np.linspace(lo, hi, len(db), endpoint=False)
        else:                                         # real sweep, segment by segment
            fs, dbs = [], []
            for s in p.segments:
                iq = await self.src.capture(s.rf_center, s.rate, self.nfft * 2, s.frontend)
                ff, dd = psd(iq, s.rate, self.nfft)
                ff = ff + s.rf_center
                k = (ff >= s.rf_lo) & (ff < s.rf_hi)
                fs.append(ff[k]); dbs.append(dd[k])
            f, db = np.concatenate(fs), np.concatenate(dbs)
        ems = detect(f, db)
        return Frame(lo, hi, rebin_max(db, self.nbins), noise_floor(db), ems,
                     time.time(), "sweep", span / len(db))
