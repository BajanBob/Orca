"""Skylark2040 GNSS adapter: gpsd or NMEA serial in; jamming and spoofing detection out."""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import dataclass, field

from ..oef import Event


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class GnssSample:
    ts: float
    cn0: list[float]                 # C/N0 (dB-Hz) of satellites in view
    lat: float | None = None
    lon: float | None = None
    used: int = 0


@dataclass
class GnssMonitor:
    """Turns raw GNSS samples into health, jamming and spoofing events."""
    node: str
    jam_drop_db: float = 10.0
    spoof_jump_m: float = 500.0
    ref_cn0: float = 42.0          # typical top-4 C/N0 under open sky (dB-Hz)
    base: float | None = None
    n: int = 0
    last: GnssSample | None = None
    state: str = "ok"
    _tick: int = field(default=0)

    @staticmethod
    def top_mean(cn0: list[float], k: int = 4) -> float:
        s = sorted(cn0, reverse=True)[:k]
        return sum(s) / len(s) if s else 0.0

    def feed(self, s: GnssSample) -> list[Event]:
        evs: list[Event] = []
        m = self.top_mean(s.cn0)
        geo = {"lat": s.lat, "lon": s.lon, "err_m": 15} if s.lat is not None else None
        if self.base is not None and self.n >= 10:
            ref, need = self.base, self.jam_drop_db
        else:   # no site baseline yet: compare with a typical open-sky value, more strictly
            ref, need = self.ref_cn0, self.jam_drop_db + 2
        jam = ref - m >= need or (s.used < 4 and m < ref - 6)
        spoof = False
        if self.last and self.last.lat is not None and s.lat is not None and not jam:
            dt = max(0.1, s.ts - self.last.ts)
            jump = haversine_m(self.last.lat, self.last.lon, s.lat, s.lon)
            if jump > self.spoof_jump_m and dt < 5:
                spoof = True
                evs.append(Event("skylark.gnss", "rf.gnss.spoofing", "high", self.node,
                                 {"jump_m": round(jump), "dt_s": round(dt, 1), "cn0_mean": round(m, 1)},
                                 geo, 0.8))
        # A real sky spreads C/N0 by elevation (typically 4-8 dB SD). A single
        # spoofing transmitter makes every satellite arrive at nearly the same level.
        if len(s.cn0) >= 8:
            mu = sum(s.cn0) / len(s.cn0)
            sd = (sum((c - mu) ** 2 for c in s.cn0) / len(s.cn0)) ** 0.5
            if sd < 1.0 and mu > 40:
                spoof = True
                evs.append(Event("skylark.gnss", "rf.gnss.spoofing", "high", self.node,
                                 {"reason": "uniform C/N0", "cn0_sd": round(sd, 2), "cn0_mean": round(m, 1)},
                                 geo, 0.7))
        if jam:
            conf = min(0.99, 0.6 + (ref - m) / 40)
            evs.append(Event("skylark.gnss", "rf.gnss.jamming", "high", self.node,
                             {"cn0_mean": round(m, 1), "cn0_base": round(ref, 1),
                              "baseline": "site" if ref is self.base else "open-sky default",
                              "drop_db": round(ref - m, 1), "sats_used": s.used}, geo, round(conf, 2)))
            self.state = "jammed"
        else:
            if not spoof:   # learn only from clean samples
                self.base = m if self.base is None else 0.95 * self.base + 0.05 * m
                self.n += 1
            self.state = "spoof?" if spoof else "ok"
        self._tick += 1
        if self._tick % 5 == 0:
            evs.append(Event("skylark.gnss", "rf.gnss.health", "info", self.node,
                             {"cn0_mean": round(m, 1), "sats_used": s.used, "state": self.state}, geo))
        self.last = s
        return evs


# ------------------------------------------------------------------ parsers
def nmea_ok(line: str) -> bool:
    if not line.startswith("$") or "*" not in line:
        return False
    body, cs = line[1:].split("*", 1)
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}" == cs[:2].upper()


def _deg(v: str, hemi: str) -> float | None:
    if not v:
        return None
    d = int(float(v) / 100)
    val = d + (float(v) - d * 100) / 60
    return -val if hemi in ("S", "W") else val


class NmeaState:
    """Accumulates GSV (C/N0) and GGA (fix) sentences into samples."""

    def __init__(self):
        self.cn0: dict[str, float] = {}
        self.lat = self.lon = None
        self.used = 0

    def feed(self, line: str) -> GnssSample | None:
        line = line.strip()
        if not nmea_ok(line):
            return None
        f = line[1:].split("*")[0].split(",")
        t = f[0][2:]
        if t == "GSV":
            talker = f[0][:2]
            for i in range(4, len(f) - 3, 4):
                prn, snr = f[i], f[i + 3]
                if prn and snr:
                    self.cn0[talker + prn] = float(snr)
        elif t == "GGA":
            self.lat, self.lon = _deg(f[2], f[3]), _deg(f[4], f[5])
            self.used = int(f[7] or 0)
            s = GnssSample(time.time(), list(self.cn0.values()), self.lat, self.lon, self.used)
            self.cn0 = {}
            return s
        return None


# ------------------------------------------------------------------ adapters
class GpsdAdapter:
    def __init__(self, node: str, host: str = "127.0.0.1", port: int = 2947):
        self.node, self.host, self.port = node, host, port
        self.mon = GnssMonitor(node)
        self.health = "connecting"

    async def run(self, emit):
        while True:
            try:
                r, w = await asyncio.open_connection(self.host, self.port)
                w.write(b'?WATCH={"enable":true,"json":true};\n')
                await w.drain()
                self.health = "online"
                cn0: list[float] = []
                used = 0
                while line := await r.readline():
                    msg = json.loads(line)
                    if msg.get("class") == "SKY":
                        sats = msg.get("satellites", [])
                        cn0 = [s["ss"] for s in sats if s.get("ss")]
                        used = sum(1 for s in sats if s.get("used"))
                    elif msg.get("class") == "TPV" and "lat" in msg:
                        for e in self.mon.feed(GnssSample(time.time(), cn0, msg["lat"], msg["lon"], used)):
                            emit(e)
            except Exception as ex:   # gpsd down or restarted: retry
                self.health = f"offline: {ex.__class__.__name__}"
                await asyncio.sleep(5)


class NmeaSerialAdapter:
    def __init__(self, node: str, device: str, baud: int = 9600):
        self.node, self.device, self.baud = node, device, baud
        self.mon, self.state = GnssMonitor(node), NmeaState()
        self.health = "connecting"

    async def run(self, emit):
        import serial  # pyserial, optional
        loop = asyncio.get_running_loop()
        while True:
            try:
                port = serial.Serial(self.device, self.baud, timeout=1)
                self.health = "online"
                while True:
                    raw = await loop.run_in_executor(None, port.readline)
                    s = self.state.feed(raw.decode("ascii", "ignore"))
                    if s:
                        for e in self.mon.feed(s):
                            emit(e)
            except Exception as ex:
                self.health = f"offline: {ex.__class__.__name__}"
                await asyncio.sleep(5)


class SimGnssAdapter:
    """Simulated receiver. Jammed while the 'gnss_jam' scenario is on and the
    node is within `jam_radius_m` of the jammer."""

    def __init__(self, node: str, lat: float, lon: float, scenarios: set,
                 jam_at: tuple[float, float] = (13.104, -59.632), jam_radius_m: float = 3000):
        self.node, self.lat, self.lon = node, lat, lon
        self.scenarios, self.jam_at, self.jam_radius = scenarios, jam_at, jam_radius_m
        self.mon = GnssMonitor(node)
        self.health = "online (sim)"

    def sample(self) -> GnssSample:
        n = random.randint(9, 12)
        cn0 = [random.gauss(44, 3.5) for _ in range(n)]
        used = n - 1
        d = haversine_m(self.lat, self.lon, *self.jam_at)
        if "gnss_jam" in self.scenarios and d < self.jam_radius:
            drop = 22 * (1 - d / self.jam_radius) + 6
            cn0 = [max(0, c - drop) for c in cn0]
            used = sum(1 for c in cn0 if c > 28)
        lat = self.lat + random.gauss(0, 2e-5)
        lon = self.lon + random.gauss(0, 2e-5)
        if "gnss_spoof" in self.scenarios:      # one transmitter: uniform power, drifting fix
            cn0 = [random.gauss(46, 0.4) for _ in range(n)]
            lat += 0.004 * (time.time() % 60)
        return GnssSample(time.time(), cn0, lat, lon, used)

    async def run(self, emit):
        while True:
            for e in self.mon.feed(self.sample()):
                emit(e)
            await asyncio.sleep(1.0)
