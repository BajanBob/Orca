"""Uranus adapter: WSJT-X UDP protocol (FT8/FT4/WSPR decodes) plus HF propagation summary.

Point WSJT-X (or JTDX / Uranus) at UDP port 2237 on the Orca server, or use the
multicast address WSJT-X is configured for.
"""
from __future__ import annotations

import asyncio
import random
import struct
import time
from collections import deque

from ..oef import Event

MAGIC = 0xADBCCBDA


class _R:
    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def take(self, fmt: str):
        v = struct.unpack_from(">" + fmt, self.b, self.i)[0]
        self.i += struct.calcsize(">" + fmt)
        return v

    def utf8(self) -> str | None:
        n = self.take("I")
        if n == 0xFFFFFFFF:
            return None
        s = self.b[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return s


def parse(datagram: bytes) -> dict | None:
    r = _R(datagram)
    try:
        if r.take("I") != MAGIC:
            return None
        r.take("I")                  # schema
        t = r.take("I")
        out = {"type": t, "id": r.utf8()}
        if t == 1:                   # Status
            out.update(dial_hz=r.take("Q"), mode=r.utf8())
        elif t == 2:                 # Decode
            out.update(new=bool(r.take("B")), ms=r.take("I"), snr=r.take("i"), dt=r.take("d"),
                       df=r.take("I"), mode=r.utf8(), message=r.utf8())
        elif t == 10:                # WSPRDecode
            out.update(new=bool(r.take("B")), ms=r.take("I"), snr=r.take("i"), dt=r.take("d"),
                       freq_hz=r.take("Q"), drift=r.take("i"), call=r.utf8(), grid=r.utf8(),
                       power_dbm=r.take("i"))
        return out
    except struct.error:
        return None


def _u(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">I", len(b)) + b


def build_decode(message: str, snr: int, df: int = 1200, mode: str = "~", app: str = "WSJT-X") -> bytes:
    """Encode a Decode packet (used by tests and the simulator)."""
    ms = int((time.time() % 86400) * 1000)
    return (struct.pack(">III", MAGIC, 2, 2) + _u(app) + struct.pack(">BIid", 1, ms, snr, 0.1)
            + struct.pack(">I", df) + _u(mode) + _u(message) + struct.pack(">BB", 0, 0))


def build_status(dial_hz: int, mode: str = "FT8", app: str = "WSJT-X") -> bytes:
    return struct.pack(">III", MAGIC, 2, 1) + _u(app) + struct.pack(">Q", dial_hz) + _u(mode)


class UranusAdapter:
    def __init__(self, node: str, mode: str = "sim", port: int = 2237, host: str = "0.0.0.0"):
        self.node, self.mode, self.port, self.host = node, mode, port, host
        self.dial_hz = 14_074_000
        self.app_mode = "FT8"
        self.recent: deque = deque(maxlen=400)   # (ts, snr) for propagation summary
        self.health = "starting"

    def handle(self, msg: dict, emit):
        if msg["type"] == 1:
            self.dial_hz, self.app_mode = msg["dial_hz"], msg.get("mode") or self.app_mode
        elif msg["type"] == 2:
            words = (msg.get("message") or "").split()
            self.recent.append((time.time(), msg["snr"]))
            emit(Event("uranus.ft8", "hf.decode", "info", self.node,
                       {"mode": self.app_mode, "freq_hz": self.dial_hz + msg["df"], "snr": msg["snr"],
                        "message": msg.get("message"),
                        "grid": words[-1] if words and len(words[-1]) == 4 and words[-1][:2].isalpha() else None}))
        elif msg["type"] == 10:
            self.recent.append((time.time(), msg["snr"]))
            emit(Event("uranus.wspr", "hf.wspr", "info", self.node,
                       {"freq_hz": msg["freq_hz"], "snr": msg["snr"], "call": msg["call"],
                        "grid": msg["grid"], "power_dbm": msg["power_dbm"], "drift": msg["drift"]}))

    def propagation(self) -> dict:
        cut = time.time() - 900
        s = [snr for t, snr in self.recent if t > cut]
        return {"decodes_15min": len(s), "median_snr": sorted(s)[len(s) // 2] if s else None,
                "band_hz": self.dial_hz}

    async def run(self, emit):
        if self.mode == "sim":
            return await self._run_sim(emit)
        loop = asyncio.get_running_loop()
        adapter = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                m = parse(data)
                if m:
                    adapter.handle(m, emit)

        await loop.create_datagram_endpoint(Proto, local_addr=(self.host, self.port))
        self.health = f"listening UDP {self.port}"
        while True:
            await asyncio.sleep(60)
            emit(Event("uranus.ft8", "hf.propagation", "info", self.node, self.propagation()))

    async def _run_sim(self, emit):
        self.health = "online (sim)"
        calls = [("CQ K1ABC FN42", -12), ("8P6KM W4XYZ EM73", -18), ("CQ EA8TL IL18", -9),
                 ("CQ PY2ZZ GG66", -15), ("CQ ZS6AA KG33", -20), ("8P6JD G4ABC IO91", -17)]
        n = 0
        while True:
            await asyncio.sleep(15 - (time.time() % 15) + 0.5)   # FT8 cycle
            for m, snr in random.sample(calls, random.randint(2, 5)):
                self.handle(parse(build_decode(m, snr + random.randint(-4, 4), random.randint(300, 2800))), emit)
            n += 1
            if n % 4 == 0:
                emit(Event("uranus.ft8", "hf.propagation", "info", self.node, self.propagation()))
