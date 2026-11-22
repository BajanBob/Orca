"""Lawful-access profile for the government / regulator edition.

This makes a spectrum regulator's *legitimate* powers explicit and auditable. It
adds governance, not a new decrypt capability:

  * It authorises access only through the existing secure module (the operator's
    OWN communications on licensed, non-amateur bands), gated by SecurePolicy
    (floor, amateur exclusion, recorded authority, licensed bands).
  * It records a cited authority / warrant reference, a named service and a time
    window for every access, and emits a tamper-evident audit entry.
  * It produces an enforcement report that flags encrypted, unlicensed and
    unidentified carriers for action.

Hard line, unchanged and enforced here too: it REFUSES any conditional-access
service (BISS, PowerVu, Conax, DigiCipher, Nagra ...). Orca identifies CA; it
never descrambles it, for any customer, under any "exemption". Lawful access to a
third party's encrypted content is obtained by operator/court-compelled KEY
PROVISION into the secure module's key store - not by circumventing a CA system.
Receive-only throughout.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import satcom
from .secure import SecurePolicy


@dataclass
class LawfulAccessProfile:
    policy: SecurePolicy
    warrant_ref: str = ""            # court order / tasking reference
    services: list[str] = field(default_factory=list)   # named services in scope
    valid_from: float = 0.0
    valid_to: float = 0.0           # 0 = open-ended within the authority
    audit: list[dict] = field(default_factory=list)

    def _in_window(self, now: float) -> bool:
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_to and now > self.valid_to:
            return False
        return True

    def authorize(self, freq_hz: float, service: str = "",
                  ca_system_id=None, now: float | None = None) -> dict:
        """Decide whether access to a service is lawfully permitted, and log it.

        Order of checks: conditional-access first (always refused), then the
        secure-module policy (floor / amateur / authority / licence), then the
        warrant scope (named service, time window).
        """
        now = time.time() if now is None else now
        rec = {"ts": now, "freq_hz": freq_hz, "service": service,
               "authority": self.policy.authority, "warrant_ref": self.warrant_ref}

        ca = satcom.identify_ca(ca_system_id)
        if ca["encrypted"]:
            rec.update(allowed=False, reason="conditional-access",
                       message=f"{ca['ca_system']} is a conditional-access system. Orca "
                               "identifies it but never descrambles it. Lawful access to "
                               "this content requires operator/court-compelled keys, not "
                               "circumvention.")
            self.audit.append(rec)
            return rec

        ok, why = self.policy.can_secure(freq_hz, "decrypt")
        if not ok:
            rec.update(allowed=False, reason="policy", message=why)
            self.audit.append(rec)
            return rec

        if not self.warrant_ref:
            rec.update(allowed=False, reason="no-warrant",
                       message="No warrant / tasking reference recorded for lawful access.")
            self.audit.append(rec)
            return rec
        if self.services and service and service not in self.services:
            rec.update(allowed=False, reason="out-of-scope",
                       message=f"'{service}' is not in the authorised service list.")
            self.audit.append(rec)
            return rec
        if not self._in_window(now):
            rec.update(allowed=False, reason="out-of-window",
                       message="Access is outside the authority's valid time window.")
            self.audit.append(rec)
            return rec

        rec.update(allowed=True, reason="authorised",
                   message="Authorised: decrypt the operator's own comms via the secure "
                           "module key store, under the recorded authority.")
        self.audit.append(rec)
        return rec


def enforcement_report(carriers: list[dict]) -> dict:
    """Flag carriers for regulatory action. Pure; no decryption.

    Each carrier: {freq_hz, licensed(bool), ca_system_id(optional), identified(bool)}.
    Spectrum policing works entirely at this layer - no content access needed.
    """
    flags = []
    for c in carriers:
        f = c.get("freq_hz")
        reasons = []
        if c.get("licensed") is False:
            reasons.append("unlicensed emission")
        ca = satcom.identify_ca(c.get("ca_system_id"))
        if ca["encrypted"]:
            reasons.append(f"{ca['ca_system']} encrypted (identify-only)")
        if c.get("identified") is False:
            reasons.append("unidentified carrier")
        if reasons:
            flags.append({"freq_hz": f, "action": "review", "reasons": reasons})
    return {
        "carriers": len(carriers),
        "flagged": len(flags),
        "items": flags,
        "note": "Spectrum enforcement is performed on RF-layer evidence; conditional-access "
                "carriers are flagged and identified, never descrambled.",
    }


def from_config(cfg: dict, region: int = 2) -> LawfulAccessProfile | None:
    """Build a profile from the `lawful` config block, or None when disabled."""
    law = cfg.get("lawful", {})
    if not law.get("enabled"):
        return None
    sec = cfg.get("secure", {})
    pol = SecurePolicy(
        region=region,
        floor_hz=float(sec.get("floor_hz", 148e6)),
        licensed_bands=[tuple(b) for b in sec.get("licensed_bands", [])],
        authority=sec.get("authority", ""),
    )
    return LawfulAccessProfile(
        policy=pol,
        warrant_ref=law.get("warrant_ref", ""),
        services=list(law.get("services", [])),
        valid_from=float(law.get("valid_from", 0) or 0),
        valid_to=float(law.get("valid_to", 0) or 0),
    )
