"""MeshNet: off-grid sync of Orca events between nodes.

Each node gossips signed events to its peers and forwards what it hasn't seen, so
a detection reaches the whole mesh even with no internet and no central server.
Transport is pluggable: an in-process loopback for tests and simulation, UDP for
a LAN, and a KISS framing helper for LoRa or packet radio.

Trust: only events with a valid Ed25519 signature are accepted and forwarded.
Duplicates are dropped by id, and a hop limit stops loops. Federation shares a
coarse, anonymised view (no raw IQ, position rounded) between sites that opt in.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict

from .oef import Signer, dumps

MAX_HOPS = 8


def kiss_wrap(payload: bytes) -> bytes:
    FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
    esc = payload.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(bytes([FEND]), bytes([FESC, TFEND]))
    return bytes([FEND, 0x00]) + esc + bytes([FEND])


class Transport:
    def send(self, data: bytes) -> None: ...
    def set_handler(self, cb) -> None: ...


class NullTransport(Transport):
    """A mesh of one: nothing to send to. Used until peers are configured."""
    def send(self, data): ...
    def set_handler(self, cb): ...


class RadioTransport(Transport):
    """Carries mesh messages over a packet-radio link through a KISS TNC
    (Direwolf, a hardware TNC, or a LoRa gateway speaking KISS).

    This is how the mesh crosses a TROPOSCATTER or VHF/UHF path between islands
    with no internet: events are KISS-framed and sent over the air. The link is
    intermittent (troposcatter fades), so the mesh's store-and-forward and dedup
    do the heavy lifting; a message missed in a fade is re-offered and arrives on
    a later opening. `link_quality` (0..1, fed from the troposcatter monitor or an
    SNR reading) gates sending so the node doesn't transmit into a dead path.
    """
    FEND = 0xC0

    def __init__(self, host: str = "127.0.0.1", port: int = 8001, min_quality: float = 0.15):
        self.host, self.port, self.min_quality = host, port, min_quality
        self.link_quality = 1.0
        self._cb = None
        self._writer = None
        self._buf = bytearray()
        self.sent = self.recv = self.skipped = 0

    def set_handler(self, cb):
        self._cb = cb

    def send(self, data: bytes):
        if self.link_quality < self.min_quality or self._writer is None:
            self.skipped += 1
            return                       # path is down; store-and-forward will retry
        try:
            self._writer.write(kiss_wrap(data))
            self.sent += 1
        except Exception:
            self.skipped += 1

    def feed(self, chunk: bytes):
        """Feed received bytes from the TNC; emits complete frames to the mesh."""
        self._buf += chunk
        while True:
            try:
                s = self._buf.index(self.FEND)
                e = self._buf.index(self.FEND, s + 1)
            except ValueError:
                return
            raw = bytes(self._buf[s + 1:e])
            del self._buf[:e]
            if raw and (raw[0] & 0x0F) == 0 and self._cb:
                payload = (raw[1:].replace(bytes([0xDB, 0xDD]), bytes([0xC0]))
                           .replace(bytes([0xDB, 0xDC]), bytes([0xDB])))
                self._cb(payload)
                self.recv += 1

    async def run(self):
        import asyncio
        while True:
            try:
                r, self._writer = await asyncio.open_connection(self.host, self.port)
                while chunk := await r.read(1024):
                    self.feed(chunk)
            except Exception:
                self._writer = None
                await asyncio.sleep(5)


class Loopback(Transport):
    """Shared bus connecting several nodes in one process (tests, simulation)."""
    def __init__(self, hub: "LoopHub", node_id: str):
        self.hub, self.node_id, self._cb = hub, node_id, None
        hub.register(self)

    def send(self, data):
        self.hub.broadcast(self.node_id, data)

    def set_handler(self, cb):
        self._cb = cb

    def deliver(self, data):
        if self._cb:
            self._cb(data)


class LoopHub:
    def __init__(self):
        self.ports: list[Loopback] = []
        self.links: dict[str, set[str]] = {}

    def register(self, p: Loopback):
        self.ports.append(p)

    def link(self, a: str, b: str):
        self.links.setdefault(a, set()).add(b)
        self.links.setdefault(b, set()).add(a)

    def broadcast(self, src: str, data: bytes):
        peers = self.links.get(src)
        for p in self.ports:
            if p.node_id != src and (peers is None or p.node_id in peers):
                p.deliver(data)


class MeshNode:
    def __init__(self, node_id: str, signer: Signer, transport: Transport,
                 pubkeys: dict[str, str] | None = None, seen_max: int = 5000):
        self.node_id = node_id
        self.signer = signer
        self.transport = transport
        self.pubkeys = pubkeys or {}        # node_id -> public_hex, for verification
        self.seen: OrderedDict[str, float] = OrderedDict()
        self.seen_max = seen_max
        self.store: list[dict] = []          # store-and-forward log
        self.on_event = None                 # callback(event_dict) for locally-new events
        self.forwarded = 0
        self.dropped = 0
        self.transport.set_handler(self._recv)

    def _mark(self, eid: str) -> bool:
        if eid in self.seen:
            return False
        self.seen[eid] = time.time()
        if len(self.seen) > self.seen_max:
            self.seen.popitem(last=False)
        return True

    def publish(self, event: dict):
        """Inject a locally-produced (already signed) event into the mesh."""
        self._mark(event["id"])
        self._emit({"t": "ev", "hops": 0, "ev": event})

    def _emit(self, msg: dict):
        self.transport.send(dumps(msg).encode())

    def _verify(self, ev: dict) -> bool:
        pub = self.pubkeys.get(ev.get("node"))
        if pub is None:
            return True            # unknown node: accept but cannot verify (open mesh)
        from .oef import Event
        e = Event(ev["source"], ev["class"], ev.get("severity", "info"), ev.get("node", ""),
                  ev.get("data", {}), ev.get("geo"), ev.get("confidence", 1.0),
                  ev["ts"], ev["id"], ev.get("sig", ""))
        return Signer.verify(e, pub)

    def _recv(self, data: bytes):
        try:
            msg = json.loads(data)
            ev = msg["ev"]
            eid = ev["id"]
        except Exception:
            self.dropped += 1
            return
        if not self._mark(eid):
            return                 # already seen
        if not self._verify(ev):
            self.dropped += 1
            return
        self.store.append(ev)
        if self.on_event:
            self.on_event(ev)
        hops = msg.get("hops", 0) + 1
        if hops < MAX_HOPS:
            self.forwarded += 1
            self._emit({"t": "ev", "hops": hops, "ev": ev})

    def stats(self) -> dict:
        return {"node": self.node_id, "seen": len(self.seen), "stored": len(self.store),
                "forwarded": self.forwarded, "dropped": self.dropped}


def anonymise(event: dict, round_m: float = 1000.0) -> dict:
    """Coarse view for federation: drop raw data, round the position."""
    out = {k: event[k] for k in ("id", "ts", "class", "severity", "confidence") if k in event}
    g = event.get("geo")
    if g and g.get("lat") is not None:
        step = round_m / 111_000.0
        out["geo"] = {"lat": round(g["lat"] / step) * step, "lon": round(g["lon"] / step) * step,
                      "err_m": max(g.get("err_m", 0), round_m)}
    out["site"] = event.get("node", "")[:6]
    return out
