"""Secure communications for the operator's OWN links.

This is standard authenticated encryption (AES-256-GCM) applied to messages the
operator sends and receives on their own secure net, with keys the operator
provisions. It protects their traffic; it is NOT a tool for reading other
parties' communications. Orca holds no capability to recover a key it was not
given or to defeat encryption it is not party to, and the decoder refuses to try.

Keys are identified by a short key id so a net can rotate keys and so received
frames name which key they need.
"""
from __future__ import annotations

import hashlib
import os
import struct
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ORC1"


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"orca-kid" + key).hexdigest()[:8]


class KeyStore:
    """Holds the operator's own symmetric keys, by key id."""

    def __init__(self):
        self._keys: dict[str, bytes] = {}

    def add(self, key: bytes) -> str:
        if len(key) not in (16, 32):
            raise ValueError("key must be 16 or 32 bytes (AES-128/256)")
        kid = key_id(key)
        self._keys[kid] = key
        return kid

    def generate(self) -> tuple[str, bytes]:
        key = AESGCM.generate_key(256)
        return self.add(key), key

    def get(self, kid: str) -> bytes | None:
        return self._keys.get(kid)

    def ids(self) -> list[str]:
        return list(self._keys)


HDR = 4 + 4 + 2 + 12          # MAGIC | kid(4) | ct_len(2) | nonce(12)


class SecureChannel:
    """Frames: MAGIC | kid(4) | ct_len(2) | nonce(12) | ciphertext+tag. The length
    lets a receiver cut the frame out of a longer capture; the key id and a
    monotonic counter bind each frame, and replays with an old counter are
    rejected."""

    def __init__(self, store: KeyStore):
        self.store = store
        self._last_ctr: dict[str, int] = {}

    def seal(self, kid: str, plaintext: bytes, aad: bytes = b"") -> bytes:
        key = self.store.get(kid)
        if key is None:
            raise KeyError("no such key id")
        ctr = int(time.time() * 1000) & ((1 << 48) - 1)
        nonce = struct.pack(">Q", ctr)[2:] + os.urandom(6)
        ct = AESGCM(key).encrypt(nonce, plaintext, aad or None)
        if len(ct) > 0xFFFF:
            raise ValueError("payload too large for one frame")
        return MAGIC + bytes.fromhex(kid) + struct.pack(">H", len(ct)) + nonce + ct

    def open(self, frame: bytes, aad: bytes = b"") -> dict:
        """Decrypt a frame from the operator's own net. Returns a status dict;
        a frame whose key we do not hold is reported, never attacked."""
        if len(frame) < HDR + 16 or frame[:4] != MAGIC:
            return {"ok": False, "reason": "not an Orca secure frame"}
        kid = frame[4:8].hex()
        ct_len = struct.unpack(">H", frame[8:10])[0]
        nonce = frame[10:22]
        ct = frame[22:22 + ct_len]
        if len(ct) < ct_len:
            return {"ok": False, "reason": "truncated frame", "key_id": kid}
        key = self.store.get(kid)
        if key is None:
            return {"ok": False, "reason": "encrypted for key id %s, which this operator does not hold" % kid,
                    "key_id": kid, "encrypted": True}
        try:
            pt = AESGCM(key).decrypt(nonce, ct, aad or None)
        except Exception:
            return {"ok": False, "reason": "authentication failed (wrong key or tampered)", "key_id": kid}
        ctr = struct.unpack(">Q", b"\0\0" + nonce[:6])[0]
        if ctr <= self._last_ctr.get(kid, -1):
            return {"ok": False, "reason": "replay: counter not newer", "key_id": kid}
        self._last_ctr[kid] = ctr
        return {"ok": True, "key_id": kid, "plaintext": pt}
