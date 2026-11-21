"""Receive-only IQ sources: SoapySDR hardware, or the built-in simulator.

Orca never transmits through these sources. Transmit actions (APRS bulletins)
go through Tradewind and are gated by licence and region policy.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import numpy as np

from .bands import FRONTENDS, GHz, MHz, kHz


class Source:
    key = "base"
    max_rate = 20e6

    async def capture(self, rf_hz: float, rate: float, n: int,
                      frontend: str = "none") -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- simulator
@dataclass
class Emitter:
    name: str
    f: float           # centre (Hz)
    bw: float          # occupied bandwidth (Hz)
    p: float           # level above noise floor (dB, at native RBW)
    kind: str = "steady"   # steady | burst | ft8 | pulse | chirp
    duty: float = 1.0
    scenario: str = ""     # only active when this scenario flag is on


DEFAULT_EMITTERS = [
    Emitter("NAA VLF (MSK)", 24.0 * kHz, 200, 30),
    Emitter("DHO38 VLF (MSK)", 23.4 * kHz, 200, 22),
    Emitter("Sferics (lightning)", 10 * kHz, 18 * kHz, 8, "burst", 0.3),
    Emitter("JJY time signal", 40 * kHz, 50, 18),
    Emitter("WWVB time signal", 60 * kHz, 50, 20),
    Emitter("AM broadcast", 790 * kHz, 10 * kHz, 35),
    Emitter("AM broadcast", 1310 * kHz, 10 * kHz, 28),
    Emitter("WEFAX (USB)", 4.3179 * MHz, 3 * kHz, 22),
    Emitter("WWV 10 MHz", 10.0 * MHz, 6 * kHz, 25),
    Emitter("WSPR window", 10.1401 * MHz, 200, 8, "burst", 0.6),
    Emitter("FT8 20 m", 14.0755 * MHz, 2.6 * kHz, 18, "ft8"),
    Emitter("FM broadcast", 98.1 * MHz, 200 * kHz, 45),
    Emitter("FM broadcast", 104.3 * MHz, 200 * kHz, 40),
    Emitter("Aviation ATIS", 127.5 * MHz, 8 * kHz, 25, "burst", 0.7),
    Emitter("APRS 144.390", 144.39 * MHz, 12 * kHz, 30, "burst", 0.2),
    Emitter("2 m repeater", 145.27 * MHz, 12 * kHz, 32, "burst", 0.5),
    Emitter("Marine DSC ch 70", 156.525 * MHz, 12 * kHz, 26, "burst", 0.08),
    Emitter("Marine ch 16", 156.8 * MHz, 12 * kHz, 30, "burst", 0.25),
    Emitter("AIS 1", 161.975 * MHz, 25 * kHz, 28, "burst", 0.4),
    Emitter("AIS 2", 162.025 * MHz, 25 * kHz, 28, "burst", 0.4),
    Emitter("LTE downlink", 731.5 * MHz, 10 * MHz, 30),
    Emitter("LoRa 915", 915.2 * MHz, 125 * kHz, 20, "burst", 0.1),
    Emitter("ADS-B 1090", 1090 * MHz, 2 * MHz, 30, "burst", 0.5),
    Emitter("Inmarsat L-band", 1541.5 * MHz, 1 * MHz, 15),
    Emitter("GPS L1 (below noise)", 1575.42 * MHz, 2.046 * MHz, 1.5),
    Emitter("Iridium", 1621 * MHz, 10 * MHz, 12, "burst", 0.4),
    Emitter("Wi-Fi ch 6", 2437 * MHz, 20 * MHz, 30, "burst", 0.5),
    Emitter("ATC radar", 2800 * MHz, 4 * MHz, 45, "pulse", 0.05),
    Emitter("C-band satellite", 3950 * MHz, 36 * MHz, 18),
    Emitter("Radio altimeter (FMCW)", 4300 * MHz, 100 * MHz, 15, "burst", 0.3),
    Emitter("Wi-Fi 5 GHz", 5240 * MHz, 80 * MHz, 25, "burst", 0.4),
    Emitter("Weather radar", 5625 * MHz, 2 * MHz, 50, "pulse", 0.02),
    Emitter("EO satellite downlink", 8200 * MHz, 150 * MHz, 12, "burst", 0.3),
    Emitter("Marine radar", 9410 * MHz, 30 * MHz, 48, "pulse", 0.08),
    Emitter("Starlink downlink", 11325 * MHz, 240 * MHz, 18),
    Emitter("Ku satellite TV", 11785 * MHz, 36 * MHz, 22),
    Emitter("Ku satellite TV", 12015 * MHz, 36 * MHz, 21),
    Emitter("Ku VSAT uplink", 14250 * MHz, 5 * MHz, 15, "burst", 0.5),
    # scenario emitters (enabled from the console or the config)
    Emitter("GNSS chirp jammer", 1575.42 * MHz, 12 * MHz, 35, "chirp", 1.0, "gnss_jam"),
    Emitter("Stuck carrier ch 16", 156.802 * MHz, 1 * kHz, 40, "steady", 1.0, "ch16_carrier"),
    Emitter("Unknown telemetry", 433.92 * MHz, 50 * kHz, 25, "burst", 0.3, "new_emitter"),
]


class SimSource(Source):
    """Synthesises IQ in the frequency domain for whatever span is tuned."""
    key = "sim"
    max_rate = 20e6

    def __init__(self, emitters=None, floor_db: float = -100.0, seed: int | None = None):
        self.emitters = list(emitters or DEFAULT_EMITTERS)
        self.floor_db = floor_db
        self.scenarios: set[str] = set()
        self.rng = np.random.default_rng(seed)

    def _active(self, e: Emitter) -> bool:
        if e.scenario and e.scenario not in self.scenarios:
            return False
        if e.kind == "ft8":
            return (time.time() % 15) < 12.6
        if e.kind in ("burst", "pulse"):
            return self.rng.random() < e.duty
        return True

    def _level(self, e: Emitter) -> float:
        if e.kind == "chirp":   # swept jammer: the occupied part moves
            return e.p
        return e.p + self.rng.normal(0, 0.8)

    async def capture(self, rf_hz, rate, n, frontend="none"):
        return self.iq(rf_hz, rate, n)

    def iq(self, fc: float, fs: float, n: int) -> np.ndarray:
        freqs = np.fft.fftfreq(n, 1 / fs)
        amp0 = 10 ** (self.floor_db / 20)
        spec = (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)) * amp0 / math.sqrt(2)
        for e in self.emitters:
            off = e.f - fc
            if off + e.bw / 2 < -fs / 2 or off - e.bw / 2 > fs / 2 or not self._active(e):
                continue
            lo, hi = off - e.bw / 2, off + e.bw / 2
            if e.kind == "chirp":   # sweep a 1/6 slice across the bandwidth over 0.5 s
                ph = (time.time() % 0.5) / 0.5
                lo = off - e.bw / 2 + ph * e.bw * 5 / 6
                hi = lo + e.bw / 6
            m = (freqs >= lo) & (freqs <= hi)
            if not m.any():
                m[np.argmin(np.abs(freqs - off))] = True
            a = amp0 * 10 ** (self._level(e) / 20)
            k = int(m.sum())
            spec[m] += (self.rng.normal(size=k) + 1j * self.rng.normal(size=k)) * a / math.sqrt(2)
        return (np.fft.ifft(spec) * math.sqrt(n)).astype(np.complex64)

    # Map an emitter to a modulation family, so narrowband captures carry a true
    # modulated waveform the classifier can recognise (the painted-spectrum path
    # above is for wide sweeps only).
    @staticmethod
    def _mod_of(e: "Emitter") -> str:
        n = e.name.lower()
        if e.kind == "chirp" or "lora" in n:
            return "chirp"
        if e.kind == "pulse" or "radar" in n or "ads-b" in n or "adsb" in n:
            return "pulse"
        if "gps" in n or "gnss" in n:
            return "empty"            # spread-spectrum, below noise
        if any(k in n for k in ("lte", "wi-fi", "wifi", "starlink", "ofdm", "5g", "ku ", "satellite tv")):
            return "ofdm"
        if any(k in n for k in ("am broadcast", "wefax", "wwv", "atis", "time signal")):
            return "am"
        if any(k in n for k in ("fm broadcast", "repeater", "marine", "inmarsat", "iridium")):
            return "fm"
        if any(k in n for k in ("aprs", "ais", "dsc", "msk", "vlf", "ft8", "wspr", "dmr", "p25")):
            return "fsk"
        return "fm"

    def modulated_iq(self, fc: float, rate: float, n: int) -> tuple[np.ndarray, str, "Emitter | None"]:
        """IQ for the strongest active emitter in the tuned span, truly modulated.
        Returns (iq, modulation_family, emitter)."""
        from .intel.synth import make
        cands = [e for e in self.emitters if abs(e.f - fc) < rate / 2 and self._active(e)]
        if not cands:
            return self.iq(fc, rate, n), "empty", None
        e = max(cands, key=lambda x: x.p)
        fam = self._mod_of(e)
        base = make(fam, n, rate, self.rng) if fam != "empty" else self.iq(fc, rate, n)
        off = e.f - fc
        if fam != "empty":
            t = np.arange(n) / rate
            base = base.astype(np.complex64) * np.exp(2j * np.pi * off * t).astype(np.complex64)
            base = base * 10 ** ((e.p - 20) / 20) + self.iq(fc, rate, n) * 0.3
        return base.astype(np.complex64), fam, e

    def span_psd(self, lo: float, hi: float, nbins: int, rbw_ref: float = 10e3) -> np.ndarray:
        """Fast path for very wide spans: power per display bin (dB), no IQ."""
        edges = np.linspace(lo, hi, nbins + 1)
        binw = (hi - lo) / nbins
        lin = np.full(nbins, 10 ** (self.floor_db / 10))
        lin *= 10 ** (self.rng.normal(0, 0.5, nbins) / 10)
        for e in self.emitters:
            if e.f + e.bw / 2 < lo or e.f - e.bw / 2 > hi or not self._active(e):
                continue
            a, b = e.f - e.bw / 2, e.f + e.bw / 2
            if e.kind == "chirp":
                a += ((time.time() % 0.5) / 0.5) * e.bw * 5 / 6
                b = a + e.bw / 6
            i0 = max(0, int(np.searchsorted(edges, a)) - 1)
            i1 = min(nbins, int(np.searchsorted(edges, b)))
            dil = min(1.0, e.bw / binw)
            lin[i0:max(i1, i0 + 1)] += 10 ** ((self.floor_db + self._level(e)) / 10) * dil
        return 10 * np.log10(lin)


# ---------------------------------------------------------------- hardware
class SoapySource(Source):
    """Any SoapySDR device (RTL-SDR, Airspy, HackRF, USRP, RX888 ...), RX only."""
    key = "soapy"

    def __init__(self, args: str, gain: float | None = None, max_rate: float = 2.4e6):
        import SoapySDR  # noqa: imported lazily so the simulator runs anywhere
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
        self._S, self._RX, self._CF32 = SoapySDR, SOAPY_SDR_RX, SOAPY_SDR_CF32
        self.dev = SoapySDR.Device(args)
        self.max_rate = max_rate
        if gain is None:
            self.dev.setGainMode(SOAPY_SDR_RX, 0, True)
        else:
            self.dev.setGain(SOAPY_SDR_RX, 0, gain)
        self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.dev.activateStream(self.stream)
        self._rate = 0.0

    def _read(self, tune_hz: float, rate: float, n: int) -> np.ndarray:
        if rate != self._rate:
            self.dev.setSampleRate(self._RX, 0, rate)
            self._rate = rate
        self.dev.setFrequency(self._RX, 0, tune_hz)
        buf = np.empty(n, np.complex64)
        # discard the samples taken while the PLL settles
        self.dev.readStream(self.stream, [np.empty(8192, np.complex64)], 8192, timeoutUs=200000)
        got = 0
        while got < n:
            sr = self.dev.readStream(self.stream, [buf[got:]], n - got, timeoutUs=500000)
            if sr.ret <= 0:
                break
            got += sr.ret
        return buf[:got]

    async def capture(self, rf_hz, rate, n, frontend="none"):
        fe = FRONTENDS[frontend]
        tune = rf_hz if fe.key == "none" else fe.to_if(rf_hz)
        iq = await asyncio.get_running_loop().run_in_executor(None, self._read, tune, rate, n)
        return np.conj(iq) if fe.inverted else iq   # an inverted LNB mirrors the spectrum

    def close(self):
        try:
            self.dev.deactivateStream(self.stream)
            self.dev.closeStream(self.stream)
        except Exception:
            pass


def make_source(cfg: dict) -> Source:
    kind = cfg.get("kind", "sim")
    if kind == "sim":
        src = SimSource(seed=cfg.get("seed"))
        src.scenarios.update(cfg.get("scenarios", []))
        return src
    if kind == "soapy":
        return SoapySource(cfg.get("args", ""), cfg.get("gain"), float(cfg.get("max_rate", 2.4e6)))
    raise ValueError(f"unknown sdr kind {kind!r}")
