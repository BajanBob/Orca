"""Windows driver / plug-and-play model for Orca's radios.

Three kinds of USB interface, three driver stories:

  * SDR dongles (HackRF, RTL-SDR) need the generic **WinUSB** driver. The
    world-class way to bind it is **libwdi** (the library Zadig is built on):
    Orca installs WinUSB programmatically, no manual Zadig step. When libwdi is
    not present Orca falls back to a precise guided Zadig flow. Linux/macOS use
    libusb and need no driver.

  * CAT / CI-V bridges (Icom IC-9100, Yaesu, Kenwood ...) ride a **CP210x /
    FTDI / Prolific** USB-UART. Windows 10/11 pull these from Windows Update
    automatically; when they don't, Orca names the official package. No
    third-party driver is bundled or required by Orca itself.

  * Audio interfaces (SignaLink USB, Digirig, CM108 fobs, and every rig with a
    built-in USB sound card) are **USB Audio Class** devices: Windows' in-box
    driver binds them with nothing to install. "SignaLink support" is therefore
    recognition + correct audio-device mapping, not a driver. CM108 PTT adds a
    HID path, also in-box.

This module reports what each attached or selected device needs and, on
Windows, can drive the WinUSB install. It never bundles proprietary drivers.
`driver_plan` is pure and unit-tested; the installer touches the OS only when
explicitly called.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass


# ------------------------------------------------------------------ driver model
WINUSB = "WinUSB"
USB_AUDIO = "USB Audio Class (in-box)"
USB_SERIAL = "USB-UART bridge (CP210x / FTDI / Prolific)"
HID = "USB HID (in-box)"
NONE = "none (software / network)"

# Official sources named for reference only - Orca bundles none of these.
BRIDGE_SOURCES = {
    "cp210x": "Silicon Labs CP210x VCP driver (silabs.com) - Icom, Yaesu FT-991A, Kenwood",
    "ftdi": "FTDI VCP driver (ftdichip.com) - many CAT/data cables",
    "prolific": "Prolific PL2303 driver (prolific.com.tw) - some Yaesu/clone cables",
}


@dataclass(frozen=True)
class DriverNeed:
    interface: str          # WINUSB | USB_AUDIO | USB_SERIAL | HID | NONE
    install: str            # "libwdi" | "zadig" | "windows-update" | "in-box" | "software"
    auto: bool              # True when Orca or Windows can do it with no manual step
    detail: str


def _sdr_need() -> DriverNeed:
    if sys.platform == "win32":
        return DriverNeed(WINUSB, "libwdi", True,
                          "Bind WinUSB. Orca installs it via libwdi (no manual Zadig); "
                          "if libwdi is absent, run Zadig once: select the device, pick "
                          "WinUSB, Replace Driver.")
    return DriverNeed(NONE, "in-box", True,
                      "libusb handles the device directly; no driver install on Linux/macOS.")


def _audio_need() -> DriverNeed:
    return DriverNeed(USB_AUDIO, "in-box", True,
                      "USB Audio Class: Windows/Linux/macOS bind it automatically. "
                      "Select the 'USB Audio CODEC' device for in/out.")


def _serial_need() -> DriverNeed:
    if sys.platform == "win32":
        return DriverNeed(USB_SERIAL, "windows-update", True,
                          "Win10/11 auto-install the CP210x/FTDI bridge via Windows Update. "
                          "If the COM port is missing, install the official VCP driver, then "
                          "it appears under Ports (COM & LPT).")
    return DriverNeed(USB_SERIAL, "in-box", True,
                      "Linux cp210x/ftdi_sio/pl2303 modules are in-kernel: the port appears "
                      "as /dev/ttyUSB* automatically.")


def driver_plan(preset: dict) -> list[DriverNeed]:
    """What a Tradewind/Orca radio preset needs on this OS. Pure: no I/O.

    Driven by the interface fields (tnc/ptt), not the model name, so a new radio
    on a known interface is covered without a code change.
    """
    tnc = (preset.get("tnc") or "none").lower()
    ptt = (preset.get("ptt") or "").upper()
    label = (preset.get("label") or "").lower()
    needs: list[DriverNeed] = []

    # SDR dongles -> WinUSB
    if "rtl-sdr" in label or "rtlsdr" in preset.get("id", "") or "sdr" in label:
        needs.append(_sdr_need())

    # Audio path: any soundcard TNC (built-in USB codec, SignaLink, Digirig, CM108)
    if tnc == "sound":
        needs.append(_audio_need())

    # Serial path: CAT/CI-V rig control, serial KISS TNC, or serial-RTS/DTR PTT
    if tnc == "kiss_serial" or ptt in ("CIV", "YAESU_CAT", "RTS", "DTR"):
        needs.append(_serial_need())

    # CM108 GPIO PTT -> HID
    if ptt == "CM108":
        needs.append(DriverNeed(HID, "in-box", True,
                                "CM108/CM119 GPIO keying over USB HID: in-box on every OS."))

    # Pure software / network paths (rigctld, APRS-IS, Direwolf/UZ7HO TCP, Bluetooth TNC)
    if not needs:
        needs.append(DriverNeed(NONE, "software", True,
                                "No USB driver: software or network link (Hamlib rigctld, "
                                "APRS-IS, a TCP sound modem, or a Bluetooth TNC)."))
    return needs


def plan_dict(preset: dict) -> dict:
    needs = driver_plan(preset)
    return {
        "id": preset.get("id"), "label": preset.get("label"),
        "plug_and_play": all(n.auto for n in needs),
        "drivers": [{"interface": n.interface, "install": n.install,
                     "auto": n.auto, "detail": n.detail} for n in needs],
    }


# Known SDR dongles that need WinUSB bound (vid, pid, name).
SDR_WINUSB_TARGETS = [
    (0x1D50, 0x6089, "HackRF One"),
    (0x0BDA, 0x2838, "RTL-SDR (RTL2838)"),
    (0x0BDA, 0x2832, "RTL-SDR (RTL2832)"),
]


def setup_report() -> str:
    """Attempt WinUSB on known SDRs and print the plan for everything else."""
    lines = ["Orca driver setup", "=" * 40]
    if sys.platform != "win32":
        lines.append("Not Windows: SDRs use libusb and CAT ports are in-kernel - "
                     "nothing to install. Plug in and run `orca --devices`.")
        return "\n".join(lines)
    lines.append("SDR dongles (WinUSB via libwdi; falls back to Zadig):")
    for vid, pid, name in SDR_WINUSB_TARGETS:
        r = install_winusb(vid, pid, name)
        mark = "OK " if r["ok"] else "-> "
        lines.append(f"  {mark}{name}: {r['message']}")
    lines.append("\nCAT bridges (Icom/Yaesu/Kenwood): Windows Update installs the "
                 "CP210x/FTDI driver; the radio then shows under Ports (COM & LPT).")
    lines.append("Audio interfaces (SignaLink, Digirig, built-in USB codecs): in-box "
                 "USB-Audio driver, nothing to install.")
    return "\n".join(lines)


# ------------------------------------------------------------------ WinUSB install
def install_winusb(vid: int, pid: int, description: str = "Orca SDR") -> dict:
    """Bind WinUSB to a device via libwdi (programmatic Zadig). Windows only.

    Returns {ok, method, message}. Never raises; on any failure it returns the
    manual Zadig fallback so the caller can show instructions. This is the only
    function here that changes the system, and only when called explicitly.
    """
    if sys.platform != "win32":
        return {"ok": True, "method": "libusb",
                "message": "No WinUSB needed on this OS; libusb drives the device directly."}
    try:
        import libwdi  # pywdi / libwdi python binding, when installed
    except Exception:
        return {"ok": False, "method": "zadig-fallback",
                "message": ("libwdi not available. Install WinUSB with Zadig: Options > "
                            "List All Devices, select the device, choose WinUSB, Replace "
                            "Driver. Run Orca as administrator afterwards.")}
    try:
        wdi = libwdi.Wdi()
        dev = wdi.prepare_device(vid, pid, description, libwdi.WINUSB)
        wdi.install_driver(dev)
        return {"ok": True, "method": "libwdi",
                "message": f"WinUSB bound to {description} ({vid:#06x}:{pid:#06x})."}
    except Exception as e:  # pragma: no cover - needs Windows + hardware
        return {"ok": False, "method": "zadig-fallback",
                "message": f"libwdi install failed ({e}); use Zadig to bind WinUSB manually."}
