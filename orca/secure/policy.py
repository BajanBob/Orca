"""Band and licence policy for the secure (government/defence) module.

Two rules decide whether Orca will encrypt, transmit or decrypt on a frequency:

1. Amateur allocations are excluded. ITU Radio Regulations and national rules
   prohibit encryption of amateur-radio communications, so the secure module
   refuses any frequency inside an amateur allocation, and below the 148 MHz
   floor the operator sets. Clear-mode monitoring of public signals is handled
   by the base platform, not here.
2. The operator must hold a licence (or lawful authority) for the band, recorded
   in the config. No licence entry, no transmit and no encryption.

These are compliance guards, not security guarantees; the operator remains
responsible for operating within their own national law and authorisations.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MHz = 1e6

# Representative amateur allocations above 30 MHz, by ITU region, in Hz.
# (Lower HF amateur bands are always excluded too; see AMATEUR_HF.)
AMATEUR_VHF_UP = {
    1: [(50e6, 52e6), (144e6, 146e6), (430e6, 440e6), (1240e6, 1300e6),
        (2300e6, 2450e6), (5650e6, 5850e6), (10e9, 10.5e9)],
    2: [(50e6, 54e6), (144e6, 148e6), (219e6, 225e6), (420e6, 450e6),
        (902e6, 928e6), (1240e6, 1300e6), (2300e6, 2450e6), (5650e6, 5925e6), (10e9, 10.5e9)],
    3: [(50e6, 54e6), (144e6, 148e6), (430e6, 440e6), (1240e6, 1300e6),
        (2300e6, 2450e6), (5650e6, 5850e6), (10e9, 10.5e9)],
}
AMATEUR_HF = [(1.8e6, 2e6), (3.5e6, 4e6), (7e6, 7.3e6), (10.1e6, 10.15e6),
              (14e6, 14.35e6), (18.068e6, 18.168e6), (21e6, 21.45e6),
              (24.89e6, 24.99e6), (28e6, 29.7e6)]


@dataclass
class SecurePolicy:
    region: int = 2
    floor_hz: float = 148 * MHz          # the operator's lower bound for secure ops
    licensed_bands: list[tuple[float, float]] = field(default_factory=list)
    authority: str = ""                  # e.g. licence / tasking reference, for the audit log

    def amateur_bands(self) -> list[tuple[float, float]]:
        return AMATEUR_HF + AMATEUR_VHF_UP.get(self.region, AMATEUR_VHF_UP[2])

    def is_amateur(self, hz: float) -> bool:
        return any(lo <= hz <= hi for lo, hi in self.amateur_bands())

    def licensed(self, hz: float) -> bool:
        return any(lo <= hz <= hi for lo, hi in self.licensed_bands)

    def can_secure(self, hz: float, action: str = "encrypt") -> tuple[bool, str]:
        """action: encrypt | decrypt | transmit. Returns (allowed, reason)."""
        if hz < self.floor_hz:
            return False, f"below the {self.floor_hz / MHz:.0f} MHz secure-ops floor"
        if self.is_amateur(hz):
            return False, "amateur allocation: encryption is not permitted here"
        if not self.authority:
            return False, "no operating authority recorded in config"
        if not self.licensed(hz):
            return False, "frequency is outside the operator's licensed bands"
        return True, "ok"
