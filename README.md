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

The builds come from `.github/workflows/build.yml` on every push to `main`; the release is named after the version in `orca/__init__.py`. The executables are not code-signed yet, so Windows SmartScreen may warn on first run. The APK is debug-signed, so Android asks you to allow installs from that source.

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
  intel/          Phase 3: modulation classifier, emitter fingerprinting, features
  mesh.py         Phase 4: signed store-and-forward mesh + federation
  df.py           Phase 4: AoA and TDOA direction-finding solvers
  secure/         government/defence module: policy, own-comms crypto, decoder
  server.py       FastAPI + WebSocket
  console/        touchscreen console (also runs in demo mode with no server)
```

## Phase 3: intelligence (built)

On-device, no cloud and no GPU:

- **Modulation classifier** (`orca/intel/`): a small NumPy neural net recognises AM, FM, FSK, PSK, OFDM-like, pulsed (radar), swept (chirp/jammer) and empty noise from a short IQ snippet (~92% on held-out synthetic signals; FM/FSK is the natural overlap). Low-confidence results are reported as "unknown" rather than guessed. Tap a signal and press **Classify** in the console.
- **Emitter fingerprinting**: each detection is matched to a library so a transmitter is recognised again ("the jammer from Tuesday"). New ones are added; you can label them.

## Phase 4: mesh and scale (built)

- **MeshNet** (`orca/mesh.py`): nodes gossip signed events to their peers and forward what they haven't seen, so a detection reaches the whole mesh with no internet. Only valid Ed25519 signatures are accepted and forwarded; duplicates and loops are dropped. Transports are pluggable (in-process, UDP, and a KISS helper for LoRa/packet radio).
- **Direction finding** (`orca/df.py`): the Phase 2 DF stub is now a real solver. Angle-of-arrival (bearings) and TDOA (arrival times) both locate an emitter by least squares from three or more nodes; ~9 m error with 30 ns timing noise in testing. The jamming playbook's DF step now returns a located fix.
- **Federation**: `anonymise()` shares a coarse view (no raw IQ, rounded position) between sites that opt in.

## Secure module (government / defence edition)

Off by default; enable under `secure:` in the config. It provides:

- **Encryption and decryption of the operator's own communications** (AES-256-GCM) on frequencies they are licensed for, with keys they provision. A received frame whose key the operator does not hold is **reported, never attacked**.
- **A pluggable signal decoder** (`orca/secure/`) that demodulates and decodes on licensed frequencies; the base platform ships a clear-mode AFSK/FSK decoder, and operators add decoders for the standards they are authorised to handle.

Two boundaries are enforced in code, not configuration:

1. **Amateur bands are excluded**, and nothing runs below the 148 MHz floor. ITU and national rules prohibit encrypting amateur-radio traffic, so secure operation is confined to non-amateur allocations the operator is licensed for.
2. **Orca does not defeat third-party encryption.** It holds no capability to recover a key it was not given or to break encryption it is not party to, and the decoder will not try. This keeps the product lawful to sell and operate.

The secure module is two-way, so transmit and encryption also require a recorded operating authority and a matching licensed band. See `orca/secure/policy.py`.

## Still to come

Hardware-in-the-loop trials on real receivers, protocol-specific decoders (P25/DMR/TETRA framing — operator-supplied), and live multi-node DF with time-synced captures.
