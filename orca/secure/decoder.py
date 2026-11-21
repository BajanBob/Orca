"""Signal decoder framework for the secure module.

A decoder turns a demodulated bitstream on a licensed, non-amateur frequency into
messages. Decoders are plugins: the base platform ships a clear-mode AFSK/FSK
decoder; operators add their own for the standards they are authorised to decode.

Boundary, enforced here and not configurable:
- Decoding runs only where SecurePolicy allows it (licensed, non-amateur, above
  the floor). Amateur and public bands are handled by the base platform in clear.
- If a frame is encrypted, the decoder decrypts it ONLY with a key the operator
  has provisioned (their own net). A frame whose key is not held is reported as
  "encrypted, key not held". The decoder never tries to guess or recover a key,
  and never attempts cryptanalysis of third-party traffic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .crypto import MAGIC, SecureChannel
from .policy import SecurePolicy


@dataclass
class Decoded:
    protocol: str
    ok: bool
    bits: int = 0
    text: str = ""
    encrypted: bool = False
    decrypted: bool = False
    reason: str = ""
    key_id: str = ""


class Decoder:
    name = "base"

    def decode(self, iq: np.ndarray, rate: float, channel: SecureChannel | None) -> Decoded:
        raise NotImplementedError


def fsk_bits(iq: np.ndarray, rate: float, baud: float) -> np.ndarray:
    """Non-coherent binary FSK/AFSK demodulation to a bit array (uint8 0/1)."""
    iq = np.asarray(iq, np.complex64)
    phase = np.unwrap(np.angle(iq))
    inst = np.diff(phase)
    sps = max(4, int(rate / baud))
    n = (len(inst) // sps) * sps
    blocks = inst[:n].reshape(-1, sps)
    # average the central half of each symbol: the edges carry the transitions
    lo, hi = sps // 4, sps - sps // 4
    sym = blocks[:, lo:hi].mean(axis=1)
    # threshold at the midpoint of the two symbol levels, not the median: an
    # unbalanced frame (long runs of one bit) would pull the median onto a level.
    m = np.median(sym)
    low, high = sym[sym <= m], sym[sym > m]
    thr = 0.5 * (low.mean() + high.mean()) if len(low) and len(high) else m
    return (sym > thr).astype(np.uint8)


def fsk_demod(iq: np.ndarray, rate: float, baud: float) -> bytes:
    bits = fsk_bits(iq, rate, baud)
    return np.packbits(bits[: (len(bits) // 8) * 8]).tobytes()


def _find_frame(bits: np.ndarray) -> bytes | None:
    """Bit-level frame sync: search every bit offset and both polarities for a
    byte-aligned Orca frame (receivers resolve FSK polarity and bit phase)."""
    for inv in (bits, 1 - bits):
        for off in range(8):
            b = inv[off:]
            data = np.packbits(b[: (len(b) // 8) * 8]).tobytes()
            i = data.find(MAGIC)
            if i >= 0:
                return data[i:]
    return None


class ClearFskDecoder(Decoder):
    """Demodulates binary FSK and looks for an Orca secure frame or printable text."""
    name = "clear-fsk"

    def __init__(self, baud: float = 1200.0):
        self.baud = baud

    def decode(self, iq, rate, channel):
        bits = fsk_bits(iq, rate, self.baud)
        nbits = len(bits)
        frame = _find_frame(bits)
        if frame is not None and channel is not None:
            res = channel.open(frame)
            if res["ok"]:
                text = res["plaintext"].decode("utf-8", "replace")
                return Decoded(self.name, True, nbits, text, True, True, "decrypted with operator key", res.get("key_id", ""))
            return Decoded(self.name, False, nbits, "", True, False, res["reason"], res.get("key_id", ""))
        if frame is not None:
            return Decoded(self.name, False, nbits, "", True, False, "secure frame present; no key channel configured")
        data = np.packbits(bits[: (nbits // 8) * 8]).tobytes()
        printable = bytes(b for b in data if 32 <= b < 127)
        if len(printable) > max(4, 0.3 * len(data)):
            return Decoded(self.name, True, nbits, printable.decode("ascii", "replace"), False, False, "clear text")
        return Decoded(self.name, False, nbits, "", False, False, "no frame or text recovered")


class DecoderBank:
    def __init__(self, policy: SecurePolicy, channel: SecureChannel | None):
        self.policy = policy
        self.channel = channel
        self._decoders: list[Decoder] = []

    def register(self, decoder: Decoder):
        self._decoders.append(decoder)

    def decoders(self) -> list[str]:
        return [d.name for d in self._decoders]

    def decode(self, freq_hz: float, iq: np.ndarray, rate: float, want: str = "decrypt") -> dict:
        ok, reason = self.policy.can_secure(freq_hz, want)
        if not ok:
            return {"allowed": False, "reason": reason, "freq_hz": freq_hz}
        out = []
        for d in self._decoders:
            r = d.decode(iq, rate, self.channel)
            out.append(r.__dict__)
            if r.ok:
                break
        return {"allowed": True, "freq_hz": freq_hz, "authority": self.policy.authority,
                "results": out}
