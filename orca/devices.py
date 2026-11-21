"""USB / serial device discovery and radio-profile matching.

Orca talks to three classes of USB hardware, each on its own path:

  * SDR front-ends (HackRF One, RTL-SDR, Airspy, RX888 ...) -> SoapySDR, RX only.
  * CAT / CI-V transceivers (Icom IC-9100, Yaesu ...) -> a virtual COM port for
    rig control, plus a USB-audio codec that feeds WSJT-X / Uranus (FT8, WEFAX).
  * Packet / KISS TNCs (built-in or Direwolf soundcard) -> the Tradewind APRS and
    mesh transports over 2 m / 70 cm.

This module enumerates what is plugged in and maps each device to the Orca
subsystem that should own it, with a ready-to-paste site.yaml fragment. It is
read-only: nothing here keys a transmitter. HackRF is marked receive-only; it is
physically TX-capable but Orca never transmits through an SDR source. Transmit on
a transceiver stays gated by callsign + licence + region policy, and encryption
stays off the amateur bands (secure module, licensed bands only).

The matching logic (`match_profile`) is pure so it is unit-testable without any
hardware; `detect()` does the I/O and degrades to an empty list when pyserial or
SoapySDR is absent.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------- radio profiles
@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    subsystem: str                 # "sdr" | "cat" | "tnc"
    bind: str                      # Orca config section this device feeds
    rx_only: bool = False
    bands: str = ""
    vid_pids: tuple = ()           # (vid, pid) pairs, ints
    desc_keywords: tuple = ()      # case-insensitive substrings in the USB description
    notes: str = ""


# USB IDs are matched first; the description keywords are the fallback, because a
# generic USB-UART bridge (CP210x / FTDI / PL2303) is shared by many rigs and the
# OS often reports the rig name in the description string.
PROFILES = (
    Profile(
        key="hackrf", label="HackRF One", subsystem="sdr", bind="sdr", rx_only=True,
        bands="1 MHz - 6 GHz (with upconverter/LNB for VLF-HF and L/C/X/Ku)",
        vid_pids=((0x1D50, 0x6089), (0x1D50, 0x604B)),
        desc_keywords=("hackrf", "great scott"),
        notes="SoapySDR driver=hackrf. 20 MS/s max; use 8-10 MS/s for a clean sweep. "
              "RX only in Orca. On Windows install the WinUSB driver with Zadig."),
    Profile(
        key="ic9100", label="Icom IC-9100", subsystem="cat", bind="uranus",
        bands="HF / 50 / 144 / 430 (+1200 MHz option), all-mode",
        vid_pids=((0x10C4, 0xEA60),),     # Silicon Labs CP210x bridge
        desc_keywords=("ic-9100", "ic9100", "cp210", "silicon labs"),
        notes="One virtual COM = CI-V rig control (addr 0x7C, set baud in menu, "
              "19200 typical). The same USB also exposes a USB-AUDIO codec: point "
              "WSJT-X / Uranus at it for FT8 / WEFAX and feed Orca over UDP 2237. "
              "Narrowband audio only - no wideband IQ, so use it for CAT + decode, "
              "not for the spectrum sweep (that is the HackRF's job)."),
    Profile(
        key="ftm510", label="Yaesu FTM-510DR", subsystem="tnc", bind="aprs",
        bands="2 m / 70 cm FM + C4FM, built-in 1200/9600 APRS TNC",
        vid_pids=((0x10C4, 0xEA60), (0x067B, 0x2303), (0x0403, 0x6001)),
        desc_keywords=("ftm-510", "ftm510", "yaesu"),
        notes="Two ways in: (1) the built-in APRS TNC via the data jack, or "
              "(2) a Direwolf soundcard TNC - either way Orca reads KISS frames. "
              "The USB-C on the head is CAT / firmware. This is the 2 m/70 cm "
              "transport for the mesh and burst chat: CLEAR by amateur rules. "
              "Encryption is never applied here; it belongs only to the secure "
              "module on licensed government/defence bands."),
)

_SUBSYS_LABEL = {"sdr": "SDR front-end", "cat": "CAT / CI-V transceiver",
                 "tnc": "Packet / KISS TNC"}


def match_profile(vid, pid, description: str = "") -> Profile | None:
    """Pure match: a (vid, pid) pair and/or a USB description -> a Profile or None."""
    d = (description or "").lower()
    if vid is not None and pid is not None:
        for p in PROFILES:
            if (int(vid), int(pid)) in p.vid_pids:
                # a shared bridge (CP210x) can belong to several rigs; let the
                # description break the tie when it names one.
                tie = [q for q in PROFILES if (int(vid), int(pid)) in q.vid_pids]
                if len(tie) > 1:
                    for q in tie:
                        if any(k in d for k in q.desc_keywords if k not in ("cp210", "silicon labs", "yaesu")):
                            return q
                return p
    for p in PROFILES:
        if any(k in d for k in p.desc_keywords):
            return p
    return None


def _yaml_fragment(profile: Profile, port: str) -> str:
    if profile.subsystem == "sdr":
        return ("sdr:\n"
                "  kind: soapy\n"
                "  args: driver=hackrf\n"
                "  max_rate: 8e6")
    if profile.bind == "uranus":
        return ("uranus:\n"
                "  mode: wsjtx   # point WSJT-X/Uranus at UDP 2237; CI-V on "
                f"{port}\n"
                "  port: 2237")
    if profile.bind == "aprs":
        # Orca reads KISS over TCP. Run Direwolf on the radio's data/USB-audio
        # cable (it turns it into a KISS socket); the built-in TNC needs a
        # serial->TCP KISS bridge. Either way Orca connects to host:port.
        return ("aprs:\n"
                "  mode: kiss\n"
                "  host: 127.0.0.1   # Direwolf KISS socket for "
                f"{port}\n"
                "  port: 8001\n"
                "  callsign: \"\"      # your licensed call enables TX; blank = RX only\n"
                "  transmit: false")
    return ""


@dataclass
class Found:
    port: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str | None = None
    profile: Profile | None = None
    kind: str = "serial"           # "serial" | "sdr"

    def as_dict(self) -> dict:
        p = self.profile
        return {
            "port": self.port,
            "description": self.description,
            "vid": self.vid, "pid": self.pid,
            "serial": self.serial_number,
            "kind": self.kind,
            "matched": p.key if p else None,
            "label": p.label if p else "Unrecognised device",
            "subsystem": _SUBSYS_LABEL.get(p.subsystem, "") if p else "",
            "bind": p.bind if p else None,
            "rx_only": p.rx_only if p else None,
            "bands": p.bands if p else "",
            "notes": p.notes if p else "",
            "config": _yaml_fragment(p, self.port) if p else "",
        }


def _serial_ports() -> list[Found]:
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    out = []
    for p in list_ports.comports():
        found = Found(port=p.device, description=p.description or "",
                      vid=p.vid, pid=p.pid, serial_number=p.serial_number)
        found.profile = match_profile(p.vid, p.pid, f"{p.description} {p.manufacturer or ''}")
        out.append(found)
    return out


def _sdr_devices() -> list[Found]:
    try:
        import SoapySDR
    except Exception:
        return []
    out = []
    try:
        for d in SoapySDR.Device.enumerate():
            info = {k: d[k] for k in d.keys()}
            driver = info.get("driver", "")
            label = info.get("label", driver or "SDR")
            prof = next((p for p in PROFILES if p.subsystem == "sdr"
                         and driver in p.key), None) or match_profile(None, None, label)
            out.append(Found(port=f"driver={driver}", description=label,
                             vid=None, pid=None,
                             serial_number=info.get("serial"),
                             profile=prof, kind="sdr"))
    except Exception:
        pass
    return out


def detect() -> list[dict]:
    """Enumerate plugged-in USB serial ports and SDRs, matched to Orca subsystems."""
    return [f.as_dict() for f in (_serial_ports() + _sdr_devices())]


def report() -> str:
    """Human-readable table for the CLI (`orca devices`)."""
    devs = detect()
    plat = {"win32": "Windows (COMx)", "linux": "Linux (/dev/ttyUSB*, /dev/ttyACM*)",
            "darwin": "macOS (/dev/cu.*)"}.get(sys.platform, sys.platform)
    lines = [f"USB / serial devices  -  {plat}", "=" * 60]
    try:
        import serial  # noqa
    except Exception:
        lines.append("pyserial not installed: run  pip install pyserial")
    if not devs:
        lines.append("No USB serial ports or SDRs detected.")
        return "\n".join(lines)
    for d in devs:
        tag = d["label"] if d["matched"] else "unrecognised"
        rx = "  [RX only]" if d.get("rx_only") else ""
        lines.append(f"\n{d['port']}  ->  {tag}{rx}")
        lines.append(f"    {d['description']}")
        if d["matched"]:
            lines.append(f"    subsystem : {d['subsystem']}  (feeds '{d['bind']}')")
            lines.append(f"    bands     : {d['bands']}")
            lines.append(f"    {d['notes']}")
            if d["config"]:
                frag = d["config"].replace("\n", "\n      ")
                lines.append(f"    site.yaml:\n      {frag}")
    return "\n".join(lines)
