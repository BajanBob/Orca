"""Band plan DC to Ku, receiver/front-end inventory, and coverage planning.

No single affordable receiver covers DC to 18 GHz. Orca plans a chain per band:
direct sampling for DC-HF, a general SDR for VHF-C, and a block downconverter
(LNB/BDC) in front of an SDR for X and Ku. A wideband analyser (e.g. a 20 GHz
unit) can replace the chains where budget allows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

kHz, MHz, GHz = 1e3, 1e6, 1e9


@dataclass(frozen=True)
class Band:
    key: str
    name: str
    lo: float
    hi: float
    uses: str


# IEEE letter bands for L and above; ITU names below 1 GHz.
BANDS: list[Band] = [
    Band("dc", "DC-VLF", 0, 30 * kHz, "Lightning sferics, VLF time/nav stations, power-line"),
    Band("lfmf", "LF/MF", 30 * kHz, 3 * MHz, "NDBs, LORAN/eLORAN, AM broadcast, maritime MF"),
    Band("hf", "HF", 3 * MHz, 30 * MHz, "Amateur HF, FT8/WSPR, WEFAX, HF EmComm, OTH radar"),
    Band("vhf", "VHF", 30 * MHz, 300 * MHz, "Aviation, marine VHF/AIS, APRS, FM, ADS-B adjacent"),
    Band("uhf", "UHF", 300 * MHz, 1 * GHz, "406 MHz beacons, LoRa, cellular, TETRA, DMR"),
    Band("l", "L band", 1 * GHz, 2 * GHz, "GNSS (L1/L2/L5), ADS-B 1090, Inmarsat, Iridium"),
    Band("s", "S band", 2 * GHz, 4 * GHz, "Wi-Fi 2.4, ATC radar, weather radar, 5G n78"),
    Band("c", "C band", 4 * GHz, 8 * GHz, "Satellite C-band, Wi-Fi 5/6E, radio altimeters"),
    Band("x", "X band", 8 * GHz, 12 * GHz, "Marine radar, weather radar, EO satellite downlink"),
    Band("ku", "Ku band", 12 * GHz, 18 * GHz, "Satellite TV/VSAT, Starlink user downlink, SATCOM"),
]
BAND_BY_KEY = {b.key: b for b in BANDS}


@dataclass(frozen=True)
class Receiver:
    key: str
    name: str
    soapy: str          # SoapySDR driver string
    lo: float           # lowest tunable/sampled frequency (Hz)
    hi: float
    rate: float         # max usable sample rate (S/s)


@dataclass(frozen=True)
class Frontend:
    """Block downconverter. rf = lo + if (or lo - if when inverted)."""
    key: str
    name: str
    rf_lo: float
    rf_hi: float
    lo_hz: float
    inverted: bool = False

    def to_if(self, rf: float) -> float:
        return (self.lo_hz - rf) if self.inverted else (rf - self.lo_hz)


RECEIVERS = {r.key: r for r in [
    Receiver("soundcard", "Sound card / VLF receiver", "audio", 0, 96 * kHz, 192e3),
    Receiver("rx888", "RX888 MkII (direct sampling)", "rx888", 1 * kHz, 64 * MHz, 64e6),
    Receiver("rtlsdr", "RTL-SDR Blog V4", "rtlsdr", 500 * kHz, 1766 * MHz, 2.4e6),
    Receiver("airspy", "Airspy R2", "airspy", 24 * MHz, 1800 * MHz, 10e6),
    Receiver("hackrf", "HackRF One (RX only)", "hackrf", 1 * MHz, 6 * GHz, 20e6),
    Receiver("b210", "Ettus USRP B210", "uhd", 70 * MHz, 6 * GHz, 56e6),
    Receiver("sm200", "Signal Hound SM200C class", "signalhound", 100 * kHz, 20 * GHz, 40e6),
    Receiver("sim", "Orca simulator", "sim", 0, 20 * GHz, 20e6),
]}

FRONTENDS = {f.key: f for f in [
    Frontend("none", "Direct", 0, 1e12, 0.0),
    Frontend("c_lnb", "C-band LNB 5.15 GHz (inverted)", 3.4 * GHz, 4.2 * GHz, 5.15 * GHz, True),
    Frontend("c_bdc", "C-band BDC 4.5 GHz", 6.0 * GHz, 8.0 * GHz, 4.5 * GHz),
    Frontend("x_bdc", "X-band BDC 7.0 GHz", 8.0 * GHz, 12.0 * GHz, 7.0 * GHz),
    Frontend("ku_lnb_low", "Ku universal LNB 9.75 GHz", 10.7 * GHz, 11.7 * GHz, 9.75 * GHz),
    Frontend("ku_lnb_high", "Ku universal LNB 10.6 GHz", 11.7 * GHz, 12.75 * GHz, 10.6 * GHz),
    Frontend("ku_bdc", "Ku BDC 11.0 GHz", 12.0 * GHz, 18.0 * GHz, 11.0 * GHz),
]}


@dataclass
class Segment:
    rf_lo: float
    rf_hi: float
    receiver: str
    frontend: str
    tune_hz: float       # frequency the receiver is tuned to (IF if downconverted)
    rate: float
    inverted: bool = False

    @property
    def rf_center(self) -> float:
        return (self.rf_lo + self.rf_hi) / 2


@dataclass
class Plan:
    segments: list[Segment] = field(default_factory=list)
    gaps: list[tuple[float, float]] = field(default_factory=list)


def _coverage(rx: Receiver, fe: Frontend) -> tuple[float, float] | None:
    """RF range a receiver reaches through a front-end."""
    if fe.key == "none":
        return rx.lo, rx.hi
    if fe.inverted:
        lo, hi = fe.lo_hz - rx.hi, fe.lo_hz - rx.lo
    else:
        lo, hi = fe.lo_hz + rx.lo, fe.lo_hz + rx.hi
    lo, hi = max(lo, fe.rf_lo), min(hi, fe.rf_hi)
    return (lo, hi) if hi > lo else None


def plan(rf_lo: float, rf_hi: float, receivers: list[str], frontends: list[str] | None = None,
         usable: float = 0.8) -> Plan:
    """Cover [rf_lo, rf_hi] with the fastest available chain at each point.

    Greedy: walk upward; at the current frequency pick the chain that reaches it
    with the highest sample rate, emit a segment of its usable bandwidth.
    """
    fes = ["none"] + [f for f in (frontends or []) if f != "none"]
    chains = []
    for rk in receivers:
        rx = RECEIVERS[rk]
        for fk in fes:
            cov = _coverage(rx, FRONTENDS[fk])
            if cov:
                chains.append((rx, FRONTENDS[fk], cov))
    out = Plan()
    f = rf_lo
    while f < rf_hi - 1:
        cands = [c for c in chains if c[2][0] <= f < c[2][1]]
        if not cands:
            nxt = min([c[2][0] for c in chains if c[2][0] > f] + [rf_hi])
            out.gaps.append((f, nxt))
            f = nxt
            continue
        rx, fe, cov = max(cands, key=lambda c: (c[0].rate, c[2][1]))
        bw = rx.rate * usable
        hi = min(f + bw, cov[1], rf_hi)
        center = (f + hi) / 2
        tune = center if fe.key == "none" else fe.to_if(center)
        out.segments.append(Segment(f, hi, rx.key, fe.key, tune, rx.rate, fe.inverted))
        f = hi
    return out


def describe_plan(p: Plan) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for s in p.segments:
        k = (s.receiver, s.frontend)
        r = rows.setdefault(k, {"receiver": RECEIVERS[s.receiver].name,
                                "frontend": FRONTENDS[s.frontend].name,
                                "rf_lo": s.rf_lo, "rf_hi": s.rf_hi, "steps": 0})
        r["rf_lo"], r["rf_hi"] = min(r["rf_lo"], s.rf_lo), max(r["rf_hi"], s.rf_hi)
        r["steps"] += 1
    return list(rows.values())
