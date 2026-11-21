# Connecting USB radios to Orca

Orca talks to three classes of USB hardware, each on a different path. Plug them
in, then run:

```
orca --devices
```

It lists every USB serial port and SDR it can see, names the radio, and prints the
`site.yaml` fragment to paste. The console exposes the same list at `GET /api/devices`.
`pip install pyserial` is required for serial-port discovery (SDRs need SoapySDR).

Nothing here keys a transmitter. The HackRF is receive-only in Orca. On a
transceiver, transmit stays gated by your callsign + `transmit: true` + region
policy, and **encryption never touches the amateur bands** — that is the secure
module's job, on licensed government/defence bands only.

---

## 1. HackRF One — the wideband SDR (RX)

This is Orca's spectrum front-end: 1 MHz–6 GHz, and with your LNB/upconverter it
reaches VLF–HF below and L/C/X/Ku above.

- **Windows:** install the WinUSB driver with **Zadig** (Options → List All Devices →
  select *HackRF One* → install **WinUSB**), plus SoapySDR with the HackRF module.
- **Linux:** `sudo apt install soapysdr-module-hackrf hackrf`; confirm with `hackrf_info`.
- It enumerates as a USB device (not a COM port), so it shows up under the SDR list.

```yaml
sdr:
  kind: soapy
  args: driver=hackrf
  max_rate: 8e6        # 8–10 MS/s is a clean sweep; 20 MS/s max
```

## 2. Icom IC-9100 — CAT + decode

The IC-9100's USB-B gives you **two things at once**:

- a **virtual COM port** = CI-V rig control (address `0x7C`, baud set in the menu,
  19200 typical) — on Windows via the Silicon Labs CP210x driver; on Linux it is
  `/dev/ttyUSB0` (or `/dev/ttyACM0`), no driver needed.
- a **USB-AUDIO codec** = RX/TX audio. Point **WSJT-X / Uranus** at it for FT8,
  FT4, WSPR and WEFAX, and feed Orca over **UDP 2237**.

It is narrowband audio only — no wideband IQ — so use it for CAT + decoding, and
let the HackRF do the spectrum sweep.

```yaml
uranus:
  mode: wsjtx          # WSJT-X/Uranus → UDP 2237; CI-V on the COM port
  port: 2237
```

## 3. Yaesu FTM-510DR — the 2 m / 70 cm transport

This carries the **mesh and burst chat** on 2 m/70 cm, and APRS. Two ways in:

- **Direwolf soundcard TNC** on the data/USB-audio cable (recommended) — Direwolf
  exposes a **KISS socket** Orca connects to.
- the radio's **built-in 1200/9600 APRS TNC** via the data jack — needs a
  serial→TCP KISS bridge, since Orca reads KISS over TCP.

The USB-C on the control head is CAT / firmware. On 2 m/70 cm the chat is **clear by
amateur rules** — Orca never encrypts here.

```yaml
aprs:
  mode: kiss
  host: 127.0.0.1      # Direwolf KISS socket
  port: 8001
  callsign: ""         # your licensed call enables TX; blank = RX only
  transmit: false
```

---

## Finding the port by hand

- **Windows:** Device Manager → *Ports (COM & LPT)* → note `COMx`. SDRs appear under
  *Universal Serial Bus devices* once Zadig has bound WinUSB.
- **Linux:** `ls /dev/ttyUSB* /dev/ttyACM*`, or `dmesg | tail` right after plugging
  in. Add yourself to the `dialout` group for access. `SoapySDRUtil --find` lists SDRs.
- **macOS:** `ls /dev/cu.*`.

One site can run all three at once — HackRF for the sweep, IC-9100 for HF decode and
CAT, FTM-510DR for the 2 m/70 cm mesh — each on its own USB path.
