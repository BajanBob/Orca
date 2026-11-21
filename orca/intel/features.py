"""Signal feature extraction for on-device modulation recognition.

The features are the ones a radio engineer would reach for: they separate AM,
FM, FSK, PSK, multi-carrier (OFDM-like), pulsed (radar), swept (chirp) and
empty noise without needing to demodulate. All run on short IQ snippets and
cost a couple of FFTs, so they work on a Raspberry Pi or a phone.
"""
from __future__ import annotations

import numpy as np

FEATURE_NAMES = [
    "papr_db", "env_cv", "spec_flatness", "occupancy", "inst_freq_std",
    "inst_freq_kurt", "phase_std", "zero_rate", "spectral_centroid",
    "spectral_rolloff", "duty", "peakiness", "if_bimodal", "spec_peaks",
]


def _norm(iq: np.ndarray) -> np.ndarray:
    iq = iq - iq.mean()
    p = np.sqrt(np.mean(np.abs(iq) ** 2)) + 1e-12
    return iq / p


def extract(iq: np.ndarray, rate: float) -> np.ndarray:
    """Return a fixed-length feature vector for one IQ snippet."""
    iq = _norm(np.asarray(iq, np.complex64))
    n = len(iq)
    env = np.abs(iq)
    env_m = env.mean() + 1e-9

    # power spectrum
    win = np.hanning(n).astype(np.float32)
    sp = np.abs(np.fft.fftshift(np.fft.fft(iq * win))) ** 2
    sp_n = sp / (sp.sum() + 1e-12)

    papr_db = 10 * np.log10((env.max() ** 2) / (np.mean(env ** 2) + 1e-12) + 1e-12)
    env_cv = env.std() / env_m                                   # AM/pulse raise this
    gmean = np.exp(np.mean(np.log(sp + 1e-12)))
    spec_flatness = gmean / (sp.mean() + 1e-12)                  # noise/OFDM -> ~1
    thr = sp.max() * 0.01
    occupancy = np.mean(sp > thr)

    # instantaneous frequency and phase
    phase = np.unwrap(np.angle(iq))
    ifreq = np.diff(phase) * rate / (2 * np.pi)
    ifreq = ifreq[np.abs(ifreq) < rate / 2]
    if_std = ifreq.std() / (rate / 2) if len(ifreq) else 0.0
    if_kurt = _kurt(ifreq) if len(ifreq) > 3 else 0.0
    # phase residual after removing linear trend (PSK has discrete jumps)
    t = np.arange(n)
    sl = np.polyfit(t, phase, 1)
    phase_std = np.std(phase - np.polyval(sl, t)) / np.pi
    zero_rate = np.mean(np.abs(np.diff(np.sign(np.real(iq)))) > 0)

    freqs = np.linspace(-0.5, 0.5, n)
    centroid = float(np.sum(freqs * sp_n))
    cumsum = np.cumsum(sp_n)
    rolloff = float(freqs[np.searchsorted(cumsum, 0.85)] if cumsum[-1] > 0 else 0)

    # time-domain duty: fraction of frames with power well above the median
    frames = env ** 2
    k = max(1, n // 64)
    block = frames[: (n // k) * k].reshape(-1, k).mean(axis=1)
    duty = float(np.mean(block > 3 * np.median(block + 1e-12)))
    peakiness = float(sp.max() / (np.median(sp) + 1e-12))
    peakiness = np.log10(peakiness + 1)

    # FSK sits at a few discrete instantaneous frequencies; FM spreads out.
    # Normalise by spread so the test is shape, not scale.
    if len(ifreq) > 16 and ifreq.std() > 0:
        z = np.clip((ifreq - ifreq.mean()) / (ifreq.std() + 1e-9), -2.5, 2.5)
        h, _ = np.histogram(z, bins=16, range=(-2.5, 2.5))
        h = h / (h.sum() + 1e-9)
        if_bimodal = float(np.sort(h)[-3:].sum())      # mass in the fullest bins
    else:
        if_bimodal = 0.0
    # number of strong spectral lines (FSK/pulse few, OFDM/noise many)
    spec_peaks = float(np.mean(sp > sp.max() * 0.25))

    return np.array([papr_db, env_cv, spec_flatness, occupancy, if_std, if_kurt,
                     phase_std, zero_rate, abs(centroid), abs(rolloff), duty,
                     peakiness, if_bimodal, spec_peaks], np.float32)


def _kurt(x: np.ndarray) -> float:
    x = x - x.mean()
    s = x.std() + 1e-12
    return float(np.mean((x / s) ** 4) - 3)
