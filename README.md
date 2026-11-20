# Orca — Phase 2: real sensors

Orca is a spectrum-intelligence and security-operations console. Phase 2 connects it to real sensors. It adds a touchscreen console that can sample bandwidth anywhere from **DC to Ku band (0–18 GHz)**, and it captures both data and screens.

With no hardware attached, everything runs on a built-in simulator. That means you can try the full system, and train operators, on any laptop.

## Downloads

Ready-built apps are attached to each [release](https://github.com/BajanBob/orca/releases):

| File | What it is |
|---|---|
| `Orca-Desktop-Windows-x64.zip` | Windows 10/11 desktop app. Unzip and run `OrcaDesktop.exe`; the console opens in its own window. Settings live in `%LOCALAPPDATA%\Orca\site.yaml`. |
| `Orca-Server-Windows-x64.zip` | `orca-server.exe`: a headless server for a control-room PC. Open `http://<pc-ip>:8088` from any tablet, phone or wall screen. |
| `Orca-Android-APK.zip` | Android 7+ touch console. Tap **Server** to connect to your Orca PC; it runs in demo mode without one. |

The builds come from `.github/workflows/build.yml` whenever a `v*` tag is pushed. The executables are not code-signed yet, so Windows SmartScreen may warn on first run. The APK is debug-signed, so Android asks you to allow installs from that source.

## Quick start

```bash
pip install -r requirements.txt
cp config/site.example.yaml config/site.yaml     # edit for your site
python -m orca --config config/site.yaml
# open http://<server-ip>:8088 on a touchscreen, tablet, phone or laptop
python -m pytest -q                              # 15 tests
```

## What Phase 2 adds

| Area | What it does |
|---|---|
| **Spectrum DC to Ku** | Any span from a few kHz up to 0–18 GHz. Narrow spans use one tune with fine resolution; wide spans are swept and stitched together. |
| **Bandwidth sampling** | For every signal it finds: centre frequency, 99 % occupied bandwidth (the ITU method), −3 dB bandwidth, SNR, noise floor and band occupancy. |
| **Coverage planner** | Works out which receiver and downconverter covers each frequency, and reports any gaps (`orca/bands.py`). |
| **Site baseline** | Learns what is normal for each view, by hour of the week, and stores it in SQLite. Signals that are far louder than normal are flagged. |
| **Skylark2040 GNSS** | Reads GNSS data from gpsd or NMEA serial. Detects jamming (C/N0 drop) and spoofing (position jumps, or every satellite at the same power). |
| **Tradewind Commander** | Receives APRS through a KISS TNC (e.g. Direwolf) or APRS-IS (receive only). Raises an alert when a station that should beacon regularly goes quiet. Sends bulletins only after operator approval. |
| **Uranus** | Listens to the WSJT-X UDP protocol for FT8/FT4/WSPR decodes and summarises HF propagation. |
| **Network** | Listens for UDP syslog and picks out authentication failures, rogue access points and scans, then links them to RF incidents. |
| **Capture** | IQ recordings in **SigMF** (the open standard, with the signals it found marked in the file), spectrum snapshots as CSV, and console screenshots. All captures can be downloaded from the console. |
| **Beryl agents** | Write a plain-language triage note for each incident. They run safe steps themselves; anything that transmits, notifies or blocks waits for an operator. Every action goes into an audit log (`audit.jsonl`). An optional Claude-written triage note can be turned on. |
| **Signed events** | Each node has its own Ed25519 key, and every event it produces is signed with it. |
| **Touch console** | Tap to measure, double-tap to zoom, pinch or drag to zoom and pan. Band rail and presets, max hold, 48 px touch targets. Works on phone, tablet and wall display. |

## Hardware: getting from DC to Ku

No single affordable receiver covers 0–18 GHz. Orca builds a chain for each band from the hardware listed under `inventory:` in the config:

| Range | Typical chain |
|---|---|
| DC – 96 kHz | Sound card or VLF receiver (`soundcard`) — Skylark VLF |
| ~1 kHz – 64 MHz | RX888 MkII direct sampling (`rx888`) |
| 1 MHz – 6 GHz | HackRF One, receive only (`hackrf`), or USRP B210 (`b210`) |
| 6 – 8 GHz | C-band block downconverter + SDR (`c_bdc`) |
| 8 – 12 GHz | X-band block downconverter + SDR (`x_bdc`) |
| 10.7 – 12.75 GHz | Universal Ku LNB (`ku_lnb_low`, `ku_lnb_high`) |
| 12 – 18 GHz | Ku block downconverter (`ku_bdc`) |
| 100 kHz – 20 GHz | One wideband analyser (`sm200`) replaces all of the above |

Real radios connect through **SoapySDR**: set `sdr: {kind: soapy, args: "driver=hackrf"}`. Orca only receives. Transmit (APRS bulletins) also needs `aprs.transmit: true` and your own licensed callsign, and every transmission is approved by an operator.

## Global use

Region packs (`orca/config.py`) hold the defaults for ITU Regions 1, 2 and 3. Distress and safety channels protected worldwide (2182 kHz, 121.5 MHz, marine channels 16 and 70, 406 MHz, GPS L1) are watched everywhere. Orca decodes only signals meant for public reception and never decrypts anything. Check your national regulator's rules before deploying.

## Simulator scenarios

Turn these on in the console or under `sdr.scenarios`: `gnss_jam`, `gnss_spoof`, `ch16_carrier`, `new_emitter`, `repeater_down`.

## Layout

```
orca/
  bands.py        band plan, receivers, downconverters, coverage planner
  sdr.py          SoapySDR source + simulator (receive only)
  spectrum.py     PSD, emission detection, bandwidth measurement, sweeps
  baseline.py     per-site learnt baseline
  oef.py          Orca Event Format + Ed25519 signing
  detect.py       rules -> incidents + playbooks
  agents.py       Beryl triage, reports, audit
  capture.py      SigMF IQ, spectrum CSV, screenshots
  adapters/       skylark_gnss, tradewind_aprs, uranus_wsjtx, network_syslog
  server.py       FastAPI + WebSocket
  console/        touchscreen console (also runs in demo mode with no server)
```

## Still to come (Phase 3–4)

On-device neural modulation/emitter classifiers, the multi-node direction-finding solver (the DF step currently queues the request), MeshNet sync over LoRa/VHF/HF, and federated sharing between sites.
