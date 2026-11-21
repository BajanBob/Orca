"""Troposcatter experiment module for 2 m / 70 cm (and up).

Two parts:

1. A link-budget calculator for a trans-horizon (troposcatter) path: scatter
   geometry, path loss, received level, SNR and a feasibility verdict. The model
   follows the practical amateur method published by Palle Preben-Hansen (OZ1RH),
   "Everyday VHF, UHF, and SHF propagation", and is calibrated to reproduce his
   validated operating points (e.g. 10 W + 20 dB combined gain ~= 290 km on 2 m).
   It is an engineering estimate for planning an experiment, not an ITU-R P.617
   certification.

2. A measurement side: Orca's receiver locks on the agreed frequency and logs the
   received SNR over time, then reports the fading statistics and the availability
   (the fraction of time the link is usable) that make troposcatter a real mode.

All of this is amateur, clear-mode operation. The secure module is excluded from
the amateur bands, so nothing here encrypts or decrypts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

C = 299_792_458.0
AE_KM = 4 / 3 * 6371.0          # 4/3-Earth effective radius (km)
# Calibration constant for the basic-transmission-loss model, fitted to OZ1RH.
_A = 69.0
_DB_PER_DEG = 10.5              # "each degree of scatter angle costs 9-12 dB"


def horizon_km(height_m: float) -> float:
    """Radio horizon distance for a 4/3-Earth, OZ1RH: d = 4.1 * sqrt(h)."""
    return 4.1 * math.sqrt(max(0.0, height_m))


def takeoff_deg(height_m: float, freq_hz: float) -> float:
    """First ground-reflection lobe elevation ~= lambda/(4h). Matches OZ1RH's
    examples (144 MHz: 10 m -> ~3 deg, 40 m -> ~0.7 deg)."""
    lam = C / freq_hz
    if height_m <= 0:
        return 0.0
    return math.degrees(lam / (4 * height_m))


def scatter_angle_deg(d_km: float, h1_m: float, h2_m: float, freq_hz: float) -> float:
    """Total scatter angle = Earth-curvature angle + both takeoff angles."""
    theta_e = math.degrees(d_km / AE_KM)
    return theta_e + takeoff_deg(h1_m, freq_hz) + takeoff_deg(h2_m, freq_hz)


def dish_gain_dbi(diameter_m: float, freq_hz: float, eff: float = 0.55) -> float:
    lam = C / freq_hz
    return 10 * math.log10(eff * (math.pi * diameter_m / lam) ** 2)


def noise_floor_dbm(bw_hz: float, nf_db: float = 3.0) -> float:
    """Thermal noise in a bandwidth plus receiver noise figure (kTB, T=290 K)."""
    return -174.0 + 10 * math.log10(bw_hz) + nf_db


# Minimum detectable SNR (dB) and typical bandwidths per mode.
MODES = {
    "cw": {"bw": 100.0, "min_snr": 0.0, "name": "CW (Morse, 100 Hz)"},
    "ssb": {"bw": 2500.0, "min_snr": 3.0, "name": "SSB voice (2.5 kHz)"},
    "ft8": {"bw": 50.0, "min_snr": -21.0, "name": "FT8 (digital, 50 Hz)"},
    "msk144": {"bw": 500.0, "min_snr": -8.0, "name": "MSK144 (meteor/tropo digital)"},
}


@dataclass
class LinkBudget:
    freq_hz: float
    d_km: float
    scatter_deg: float
    basic_loss_db: float
    gain_t: float
    gain_r: float
    eff_gain_db: float
    ptx_dbm: float
    prx_dbm: float
    mode: str
    noise_dbm: float
    snr_db: float
    margin_db: float
    verdict: str

    def to_dict(self):
        d = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}
        return d


def _eff_combined_gain(gt: float, gr: float) -> float:
    """Aperture-to-medium coupling loss: beyond ~25-30 dB at each end, extra gain
    gives diminishing returns (OZ1RH). Model the combined gain with a soft knee."""
    g = gt + gr
    knee = 50.0
    return g if g <= knee else knee + 0.45 * (g - knee)


def basic_transmission_loss_db(freq_hz: float, d_km: float, scatter_deg: float) -> float:
    f_mhz = freq_hz / 1e6
    return (_A + 20 * math.log10(f_mhz) + _DB_PER_DEG * scatter_deg
            + 10 * math.log10(max(1.0, d_km)))


def link_budget(freq_hz: float, d_km: float, ptx_w: float, gain_t_dbi: float,
                gain_r_dbi: float, h1_m: float = 30.0, h2_m: float = 30.0,
                mode: str = "ssb", nf_db: float = 3.0) -> LinkBudget:
    m = MODES.get(mode, MODES["ssb"])
    theta = scatter_angle_deg(d_km, h1_m, h2_m, freq_hz)
    lb = basic_transmission_loss_db(freq_hz, d_km, theta)
    eff_g = _eff_combined_gain(gain_t_dbi, gain_r_dbi)
    ptx_dbm = 10 * math.log10(ptx_w * 1000.0)
    prx = ptx_dbm + eff_g - lb
    noise = noise_floor_dbm(m["bw"], nf_db)
    snr = min(40.0, prx - noise)          # pegged above +40 dB (very strong)
    margin = snr - m["min_snr"]
    if margin >= 10:
        verdict = "reliable"
    elif margin >= 3:
        verdict = "workable"
    elif margin >= -3:
        verdict = "marginal (good conditions only)"
    else:
        verdict = "not feasible with this setup"
    return LinkBudget(freq_hz, d_km, theta, lb, gain_t_dbi, gain_r_dbi, eff_g,
                      ptx_dbm, prx, mode, noise, snr, margin, verdict)


# ---------------------------------------------------------------- measurement
@dataclass
class TropoStats:
    samples: int
    median_snr: float
    mean_snr: float
    best_snr: float
    worst_snr: float
    fade_depth_db: float          # median - 10th percentile (fast-fade depth)
    availability_pct: float       # fraction of time SNR >= the mode threshold
    threshold_db: float

    def to_dict(self):
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in self.__dict__.items()}


class TropoExperiment:
    """Monitors the received SNR on the experiment frequency and keeps a time
    series, computing fading statistics and availability. In simulation it
    produces a realistic troposcatter fading series around the predicted level;
    with real hardware the server feeds it the measured SNR from the SDR."""

    def __init__(self, name: str, freq_hz: float, d_km: float, mode: str = "ssb",
                 predicted: LinkBudget | None = None, keep: int = 3600):
        self.name = name
        self.freq_hz = freq_hz
        self.d_km = d_km
        self.mode = mode
        self.predicted = predicted
        self.threshold = MODES.get(mode, MODES["ssb"])["min_snr"]
        self.series: list[tuple[float, float]] = []     # (ts, snr_db)
        self.keep = keep

    def add(self, ts: float, snr_db: float):
        self.series.append((ts, snr_db))
        if len(self.series) > self.keep:
            self.series = self.series[-self.keep:]

    def stats(self) -> TropoStats | None:
        if not self.series:
            return None
        import numpy as np
        s = np.array([v for _, v in self.series])
        med = float(np.median(s))
        p10 = float(np.percentile(s, 10))
        avail = float(np.mean(s >= self.threshold) * 100)
        return TropoStats(len(s), med, float(s.mean()), float(s.max()), float(s.min()),
                          med - p10, avail, self.threshold)


def link_quality(snr_db: float, threshold_db: float) -> float:
    """Map a measured SNR to a 0..1 link quality for the mesh transport: 0 at the
    detection threshold, rising to 1 about 20 dB above it."""
    return max(0.0, min(1.0, (snr_db - threshold_db) / 20.0))


def sim_fading_snr(predicted_snr: float, t: float, rng, climate_sd_db: float = 7.0) -> float:
    """A realistic troposcatter fading sample around a predicted median SNR:
    slow log-normal variation (hours) plus fast Rayleigh fading (seconds)."""
    slow = climate_sd_db * math.sin(t / 1800.0) * 0.4 + rng.normal(0, climate_sd_db * 0.3)
    rayleigh_power = rng.exponential(1.0)             # Rayleigh amplitude -> exp power
    fast = 10 * math.log10(rayleigh_power + 1e-3)
    return predicted_snr + slow + fast
