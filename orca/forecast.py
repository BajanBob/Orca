"""Forecasting: warn before things happen.

Three on-device forecasts, all deterministic and explainable:

- Spectrum-anomaly risk: from the learnt baseline's recent trend and the hour of
  the week, project how likely an anomaly is in the near term.
- Emitter reappearance: for a periodic emitter, predict when it will next appear
  from its observed interval.
- Troposcatter propagation: a diurnal/seasonal model of when trans-horizon paths
  are most likely to open (best around dawn and dusk and in settled high pressure),
  with a hook for real space-weather / pressure data.
"""
from __future__ import annotations

import math
import time


def anomaly_risk(recent_scores: list[float], hour_of_week: int) -> dict:
    """recent_scores: recent max z-scores from the baseline. Returns 0..1 risk."""
    if not recent_scores:
        return {"risk": 0.0, "basis": "no baseline yet"}
    trend = sum(recent_scores[-10:]) / min(10, len(recent_scores))
    slope = (recent_scores[-1] - recent_scores[0]) / max(1, len(recent_scores))
    risk = max(0.0, min(1.0, (trend - 2) / 6 + slope * 0.1))
    return {"risk": round(risk, 2),
            "basis": f"recent deviation {trend:.1f}σ, trend {'rising' if slope > 0.05 else 'steady'}"}


def next_appearance(intervals_s: list[float], last_seen: float) -> dict | None:
    """Predict the next appearance of a periodic emitter from past intervals."""
    if len(intervals_s) < 2:
        return None
    mean = sum(intervals_s) / len(intervals_s)
    var = sum((x - mean) ** 2 for x in intervals_s) / len(intervals_s)
    jitter = math.sqrt(var)
    if jitter > mean * 0.5:
        return {"periodic": False, "note": "irregular; no reliable prediction"}
    eta = last_seen + mean - time.time()
    return {"periodic": True, "period_s": round(mean, 1), "jitter_s": round(jitter, 1),
            "eta_s": round(eta, 1)}


def tropo_opening(ts: float, lat: float = 13.1, pressure_hpa: float | None = None,
                  trend_hpa: float | None = None) -> dict:
    """Likelihood (0..1) that a troposcatter path is enhanced now.

    Diurnal: best near dawn and dusk (temperature inversions), worst midday.
    Pressure: settled high pressure (>1020 hPa, steady/rising) favours ducting and
    enhancement; falling pressure disfavours it. Pass real barometer data when you
    have it; otherwise the diurnal model is used alone.
    """
    hour = (time.gmtime(ts).tm_hour + lat * 0 - 4) % 24          # local-ish (UTC-4 Barbados)
    # two gentle peaks near 06:00 and 18:00
    diurnal = 0.5 + 0.35 * (math.cos((hour - 6) / 24 * 2 * math.pi) +
                            math.cos((hour - 18) / 24 * 2 * math.pi)) / 2
    score = max(0.1, min(1.0, diurnal))
    note = "diurnal model (dawn/dusk favoured)"
    if pressure_hpa is not None:
        hp = max(-0.3, min(0.3, (pressure_hpa - 1013) / 40))
        tr = 0.0 if trend_hpa is None else max(-0.15, min(0.15, trend_hpa * 0.1))
        score = max(0.05, min(1.0, score + hp + tr))
        note = f"diurnal + pressure {pressure_hpa:.0f} hPa"
    return {"opening": round(score, 2), "basis": note,
            "verdict": "enhanced" if score > 0.66 else "normal" if score > 0.4 else "poor"}
