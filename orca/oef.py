"""Orca Event Format (OEF): one signed event schema for RF and cyber events."""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SEVERITIES = ("info", "low", "medium", "high", "critical")


def _jsonable(o):
    if hasattr(o, "item"):  # numpy scalar
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_jsonable)


@dataclass
class Event:
    source: str                 # e.g. "skylark.gnss"
    cls: str                    # e.g. "rf.gnss.jamming"
    severity: str = "info"
    node: str = ""
    data: dict = field(default_factory=dict)
    geo: dict | None = None     # {"lat":..,"lon":..,"err_m":..}
    confidence: float = 1.0
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: "evt_" + uuid.uuid4().hex[:16])
    sig: str = ""

    def body(self) -> bytes:
        d = asdict(self)
        d.pop("sig")
        return dumps(d).encode()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("cls")
        return json.loads(dumps(d))

    def text(self) -> str:
        return f"{self.source} {self.cls} {self.severity} {self.node} {dumps(self.data)}".lower()


class Signer:
    """Per-node Ed25519 key. Generated on first run and kept in the data dir."""

    def __init__(self, key_path: str | os.PathLike | None = None):
        self.key_path = Path(key_path) if key_path else None
        if self.key_path and self.key_path.exists():
            self._key = serialization.load_pem_private_key(self.key_path.read_bytes(), None)
        else:
            self._key = Ed25519PrivateKey.generate()
            if self.key_path:
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                self.key_path.write_bytes(self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()))
                os.chmod(self.key_path, 0o600)
        self.public: Ed25519PublicKey = self._key.public_key()

    @property
    def public_hex(self) -> str:
        return self.public.public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw).hex()

    def sign(self, ev: Event) -> Event:
        ev.sig = "ed25519:" + base64.b64encode(self._key.sign(ev.body())).decode()
        return ev

    @staticmethod
    def verify(ev: Event, public_hex: str) -> bool:
        if not ev.sig.startswith("ed25519:"):
            return False
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        try:
            pub.verify(base64.b64decode(ev.sig[8:]), ev.body())
            return True
        except Exception:
            return False
