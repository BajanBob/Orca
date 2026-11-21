"""Synthetic labelled signals to train and test the on-device classifier.

These are training aids, not transmitters: they generate baseband IQ in memory
so the classifier can learn what each modulation looks like without needing a
library of real recordings. Real deployments can fine-tune on captured SigMF.
"""
from __future__ import annotations

import numpy as np

CLASSES = ["empty", "am", "fm", "fsk", "psk", "ofdm", "pulse", "chirp"]


def _noise(n, rng, p=0.05):
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * p


def make(cls: str, n: int, rate: float, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / rate
    snr = rng.uniform(0.5, 6)          # amplitude of signal over the 0.05 noise
    if cls == "empty":
        return _noise(n, rng, p=rng.uniform(0.03, 0.12))
    if cls == "am":
        fc = rng.uniform(-0.2, 0.2) * rate
        m = 0.6 * np.sin(2 * np.pi * rng.uniform(500, 3000) * t)
        sig = (1 + m) * np.exp(2j * np.pi * fc * t)
    elif cls == "fm":
        fc = rng.uniform(-0.2, 0.2) * rate
        dev = rng.uniform(2e3, 15e3)
        msg = np.cumsum(np.sin(2 * np.pi * rng.uniform(300, 2500) * t)) / rate
        sig = np.exp(2j * np.pi * (fc * t + dev * msg))
    elif cls == "fsk":
        fc = rng.uniform(-0.15, 0.15) * rate
        baud = rng.uniform(600, 4800)
        sym = rng.integers(0, 2, int(n * baud / rate) + 1)
        idx = (np.arange(n) * baud / rate).astype(int)
        dev = baud
        inst = fc + np.where(sym[idx] > 0, dev, -dev)
        sig = np.exp(2j * np.pi * np.cumsum(inst) / rate)
    elif cls == "psk":
        fc = rng.uniform(-0.1, 0.1) * rate
        baud = rng.uniform(1200, 9600)
        sym = rng.integers(0, 2, int(n * baud / rate) + 2) * 2 - 1
        idx = (np.arange(n) * baud / rate).astype(int)
        sig = sym[idx] * np.exp(2j * np.pi * fc * t)
    elif cls == "ofdm":
        k = rng.integers(16, 64)
        bw = rng.uniform(0.3, 0.8)
        car = (np.linspace(-bw / 2, bw / 2, k) * rate)
        sig = np.zeros(n, complex)
        for f in car:
            ph = rng.uniform(0, 2 * np.pi)
            sig += np.exp(2j * np.pi * f * t + 1j * ph) * (rng.standard_normal() + 1j * rng.standard_normal())
        sig /= np.sqrt(k)
    elif cls == "pulse":
        fc = rng.uniform(-0.2, 0.2) * rate
        pri = rng.uniform(1e-4, 5e-4)
        pw = pri * rng.uniform(0.03, 0.12)
        gate = (t % pri) < pw
        sig = gate * np.exp(2j * np.pi * fc * t)
    elif cls == "chirp":
        bw = rng.uniform(0.3, 0.9) * rate
        period = rng.uniform(2e-4, 1e-3)
        ph = (t % period) / period
        inst = -bw / 2 + ph * bw
        sig = np.exp(2j * np.pi * np.cumsum(inst) / rate)
    else:
        raise ValueError(cls)
    sig = sig.astype(np.complex64) * snr
    return sig + _noise(n, rng)


def dataset(per_class: int, n: int, rate: float, seed: int = 0):
    rng = np.random.default_rng(seed)
    X, y = [], []
    from .features import extract
    for ci, c in enumerate(CLASSES):
        for _ in range(per_class):
            X.append(extract(make(c, n, rate, rng), rate))
            y.append(ci)
    return np.array(X, np.float32), np.array(y, np.int64)
