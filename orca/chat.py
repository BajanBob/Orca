"""Burst chat: short text messages over the Orca mesh, for off-grid use.

Messages are OEF events, so they inherit the mesh's signing, dedup and
store-and-forward: a message sent during a troposcatter fade waits in the radio
transport's queue and is delivered when the path reopens ("burst" during an
opening).

Two channels, with the same encryption boundary the rest of Orca enforces:

- CLEAR channel (amateur 2 m/70 cm and any clear link): plain text. Encryption is
  prohibited on amateur bands, so ham chat is always clear.
- SECURE channel (government/defence/EmComm module): the text is encrypted with
  the operator's own AES-256-GCM key before it becomes the event payload, so even
  relay nodes can't read it. Sending is gated by SecurePolicy — licensed,
  non-amateur, above the floor, with an operating authority. A received secure
  message whose key the operator does not hold is shown as "encrypted, key not
  held", never attacked.
"""
from __future__ import annotations

import time

from .oef import Event, Signer
from .secure.crypto import SecureChannel
from .secure.policy import SecurePolicy


class BurstChat:
    def __init__(self, node: str, callsign: str, signer: Signer, publish,
                 secure_channel: SecureChannel | None = None,
                 policy: SecurePolicy | None = None, keep: int = 300):
        self.node = node
        self.callsign = callsign or node
        self.signer = signer
        self.publish = publish                # callback(event_dict) -> inject into mesh
        self.sc = secure_channel
        self.policy = policy
        self.log: list[dict] = []
        self.keep = keep

    # ------------------------------------------------------------ sending
    def send(self, text: str, freq_hz: float, secure: bool = False,
             key_id: str | None = None) -> dict:
        text = text[:200]
        if secure:
            if not (self.sc and self.policy):
                return {"ok": False, "reason": "secure module is not enabled"}
            ok, why = self.policy.can_secure(freq_hz, "transmit")
            if not ok:
                return {"ok": False, "reason": why}
            kid = key_id or (self.sc.store.ids()[0] if self.sc.store.ids() else None)
            if not kid:
                return {"ok": False, "reason": "no operator key provisioned"}
            frame = self.sc.seal(kid, text.encode("utf-8"))
            ev = Event("orca.chat", "chat.secure", "info", self.node,
                       {"from": self.callsign, "freq_hz": freq_hz, "kid": kid,
                        "frame_hex": frame.hex()})
        else:
            ev = Event("orca.chat", "chat.clear", "info", self.node,
                       {"from": self.callsign, "freq_hz": freq_hz, "text": text})
        self.signer.sign(ev)
        d = ev.to_dict()
        self._record({"ts": ev.ts, "from": self.callsign, "node": self.node, "text": text,
                      "secure": secure, "freq_hz": freq_hz, "dir": "out",
                      "status": "encrypted" if secure else "clear"})
        self.publish(d)
        return {"ok": True, "id": ev.id, "secure": secure}

    # ------------------------------------------------------------ receiving
    def on_event(self, ev: dict) -> bool:
        """Handle a chat event arriving from the mesh. Returns True if it was chat."""
        cls = ev.get("class", "")
        if cls == "chat.clear":
            d = ev["data"]
            self._record({"ts": ev["ts"], "from": d.get("from", ev.get("node", "?")),
                          "node": ev.get("node"), "text": d.get("text", ""), "secure": False,
                          "freq_hz": d.get("freq_hz"), "dir": "in", "status": "clear"})
            return True
        if cls == "chat.secure":
            d = ev["data"]
            frm = d.get("from", ev.get("node", "?"))
            if self.sc is None:
                text, status = "", "secure module not enabled"
            else:
                res = self.sc.open(bytes.fromhex(d["frame_hex"]))
                if res.get("ok"):
                    text, status = res["plaintext"].decode("utf-8", "replace"), "decrypted"
                else:
                    text, status = "", res.get("reason", "encrypted, key not held")
            self._record({"ts": ev["ts"], "from": frm, "node": ev.get("node"), "text": text,
                          "secure": True, "freq_hz": d.get("freq_hz"), "dir": "in",
                          "status": status})
            return True
        return False

    def _record(self, m: dict):
        self.log.append(m)
        if len(self.log) > self.keep:
            self.log = self.log[-self.keep:]

    def recent(self, n: int = 60) -> list[dict]:
        return self.log[-n:]
