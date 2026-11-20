"""Tradewind Commander adapter: APRS over KISS TNC (e.g. Direwolf) or APRS-IS.

Receive is always on. Transmit (bulletins) only works when the config gives a
licensed callsign and `transmit: true`, and the region pack allows it; every
transmit is an operator-approved action.
"""
from __future__ import annotations

import asyncio
import random
import re
import time

from ..oef import Event

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


# ------------------------------------------------------------------ KISS / AX.25
def kiss_unescape(data: bytes) -> bytes:
    out, esc = bytearray(), False
    for b in data:
        if esc:
            out.append(FEND if b == TFEND else FESC if b == TFESC else b)
            esc = False
        elif b == FESC:
            esc = True
        else:
            out.append(b)
    return bytes(out)


def kiss_escape(data: bytes) -> bytes:
    return data.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(bytes([FEND]), bytes([FESC, TFEND]))


def kiss_frames(buf: bytearray):
    """Pop complete frames from a byte buffer. Yields AX.25 payloads (port 0 data)."""
    while True:
        try:
            s = buf.index(FEND)
            e = buf.index(FEND, s + 1)
        except ValueError:
            return
        raw = bytes(buf[s + 1:e])
        del buf[:e]
        if raw and (raw[0] & 0x0F) == 0:
            yield kiss_unescape(raw[1:])


def _addr(b: bytes) -> str:
    call = "".join(chr(c >> 1) for c in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


def ax25_decode(frame: bytes) -> dict | None:
    if len(frame) < 16:
        return None
    dest, src = _addr(frame[0:7]), _addr(frame[7:14])
    i, path = 14, []
    last = frame[13] & 1
    while not last and i + 7 <= len(frame):
        path.append(_addr(frame[i:i + 7]))
        last = frame[i + 6] & 1
        i += 7
    if i + 2 > len(frame) or frame[i] != 0x03 or frame[i + 1] != 0xF0:
        return None
    return {"src": src, "dest": dest, "path": path,
            "info": frame[i + 2:].decode("latin-1", "replace")}


def _enc_addr(call: str, last: bool) -> bytes:
    c, _, s = call.upper().partition("-")
    b = bytes((ord(ch) << 1) for ch in c.ljust(6)[:6])
    return b + bytes([0x60 | ((int(s or 0) & 0x0F) << 1) | (1 if last else 0)])


def ax25_encode(src: str, dest: str, info: str, path: list[str] | None = None) -> bytes:
    path = path or []
    out = _enc_addr(dest, False) + _enc_addr(src, not path)
    for i, p in enumerate(path):
        out += _enc_addr(p, i == len(path) - 1)
    return out + b"\x03\xf0" + info.encode("latin-1")


# ------------------------------------------------------------------ APRS
POS_RE = re.compile(r"[!=/@](?:\d{6}[hz/])?(\d{4}\.\d{2})([NS])(.)(\d{5}\.\d{2})([EW])(.)")


def aprs_parse(src: str, info: str) -> dict:
    out: dict = {"src": src, "raw": info}
    m = POS_RE.match(info)
    if m:
        la, ns, _, lo, ew, sym = m.groups()
        lat = int(la[:2]) + float(la[2:]) / 60
        lon = int(lo[:3]) + float(lo[3:]) / 60
        out.update(kind="position", lat=-lat if ns == "S" else lat,
                   lon=-lon if ew == "W" else lon, symbol=sym,
                   comment=info[m.end():].strip())
    elif info.startswith(":") and len(info) > 11 and info[10] == ":":
        out.update(kind="message", to=info[1:10].strip(), text=info[11:])
    elif info.startswith(">"):
        out.update(kind="status", text=info[1:])
    else:
        out.update(kind="other")
    return out


def aprs_position_info(lat: float, lon: float, comment: str = "", sym: str = "/-") -> str:
    la = f"{int(abs(lat)):02d}{(abs(lat) % 1) * 60:05.2f}{'N' if lat >= 0 else 'S'}"
    lo = f"{int(abs(lon)):03d}{(abs(lon) % 1) * 60:05.2f}{'E' if lon >= 0 else 'W'}"
    return f"!{la}{sym[0]}{lo}{sym[1]}{comment}"


# ------------------------------------------------------------------ adapter
class AprsAdapter:
    """mode: kiss (host/port of a TNC like Direwolf) | aprsis (read-only) | sim."""

    def __init__(self, node: str, mode: str = "sim", host: str = "127.0.0.1", port: int = 8001,
                 callsign: str = "", transmit: bool = False, aprsis_filter: str = "r/13.1/-59.6/100",
                 watch: list[dict] | None = None, scenarios: set | None = None):
        self.node, self.mode, self.host, self.port = node, mode, host, port
        self.callsign, self.transmit = callsign.upper(), transmit
        self.filter = aprsis_filter
        self.watch = {w["call"].upper(): {"every": w.get("every_s", 600), "last": time.time(),
                                          "missing": False} for w in (watch or [])}
        self.scenarios = scenarios if scenarios is not None else set()
        self.health = "starting"
        self._writer = None

    def can_transmit(self) -> tuple[bool, str]:
        if not self.transmit:
            return False, "transmit disabled in config"
        if not self.callsign or self.callsign.startswith("N0CALL"):
            return False, "no licensed callsign configured"
        if self.mode != "kiss":
            return False, "transmit needs a KISS TNC"
        return True, "ok"

    async def send_bulletin(self, text: str, bln: str = "BLN1ORCA") -> str:
        ok, why = self.can_transmit()
        if not ok:
            return f"not sent: {why}"
        info = f":{bln:<9}:{text[:67]}"
        frame = ax25_encode(self.callsign, "APZORC", info, ["WIDE1-1", "WIDE2-1"])
        self._writer.write(bytes([FEND, 0x00]) + kiss_escape(frame) + bytes([FEND]))
        await self._writer.drain()
        return "sent"

    def _handle(self, src: str, info: str, emit):
        p = aprs_parse(src, info)
        base = src.split("-")[0]
        for call, w in self.watch.items():
            if call in (src, base):
                w["last"] = time.time()
                if w["missing"]:
                    w["missing"] = False
                    emit(Event("tradewind.aprs", "aprs.beacon.restored", "info", self.node, {"call": call}))
        geo = {"lat": p["lat"], "lon": p["lon"], "err_m": 30} if p.get("kind") == "position" else None
        emit(Event("tradewind.aprs", f"aprs.{p['kind']}", "info", self.node,
                   {k: v for k, v in p.items() if k not in ("lat", "lon")}, geo))

    async def _watchdog(self, emit):
        while True:
            now = time.time()
            for call, w in self.watch.items():
                if not w["missing"] and now - w["last"] > 3 * w["every"]:
                    w["missing"] = True
                    emit(Event("tradewind.aprs", "aprs.beacon.missing", "medium", self.node,
                               {"call": call, "silent_s": round(now - w["last"])}))
            await asyncio.sleep(5)

    async def run(self, emit):
        asyncio.create_task(self._watchdog(emit))
        if self.mode == "kiss":
            await self._run_kiss(emit)
        elif self.mode == "aprsis":
            await self._run_aprsis(emit)
        else:
            await self._run_sim(emit)

    async def _run_kiss(self, emit):
        while True:
            try:
                r, self._writer = await asyncio.open_connection(self.host, self.port)
                self.health = "online (KISS)"
                buf = bytearray()
                while chunk := await r.read(1024):
                    buf += chunk
                    for fr in kiss_frames(buf):
                        d = ax25_decode(fr)
                        if d:
                            self._handle(d["src"], d["info"], emit)
            except Exception as ex:
                self.health = f"offline: {ex.__class__.__name__}"
                await asyncio.sleep(5)

    async def _run_aprsis(self, emit):
        call = self.callsign or "N0CALL"
        while True:
            try:
                r, w = await asyncio.open_connection("rotate.aprs2.net", 14580)
                w.write(f"user {call} pass -1 vers Orca 0.2 filter {self.filter}\r\n".encode())
                await w.drain()
                self.health = "online (APRS-IS, receive only)"
                while line := await r.readline():
                    s = line.decode("latin-1").strip()
                    if not s or s.startswith("#") or ">" not in s or ":" not in s:
                        continue
                    hdr, info = s.split(":", 1)
                    self._handle(hdr.split(">")[0], info, emit)
            except Exception as ex:
                self.health = f"offline: {ex.__class__.__name__}"
                await asyncio.sleep(10)

    async def _run_sim(self, emit):
        self.health = "online (sim)"
        stations = [("8P6AB-9", 13.19, -59.54), ("8P6RT-7", 13.10, -59.61), ("8P6KM", 13.25, -59.64),
                    ("8P6JD-5", 13.07, -59.55)]
        while True:
            call, la, lo = random.choice(stations)
            if not (call.startswith("8P6RT") and "repeater_down" in self.scenarios):
                self._handle(call, aprs_position_info(la + random.gauss(0, .002), lo + random.gauss(0, .002),
                                                      "Tradewind net"), emit)
            if random.random() < 0.15:
                self._handle("8P6KM", ":ALL      :Holetown EmComm net check-in 8P6KM", emit)
            await asyncio.sleep(random.uniform(3, 7))
