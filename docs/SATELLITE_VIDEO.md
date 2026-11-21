# Free-to-air satellite video

Orca can play a **free-to-air** DVB-S/S2 channel live in the console's Live monitor.
It orchestrates standard open tools — it bundles none of them — and **never
descrambles conditional-access** (BISS, PowerVu, Conax, DigiCipher, Nagra …): those
are identified and the decode is refused.

## Pipeline

```
SDR (hackrf_transfer / rtl_sdr)  ->  DVB-S2 demod (leandvb or gr-dvbs2rx)
   ->  MPEG-TS  ->  ffmpeg  ->  live WebM (VP8/Opus)  ->  <video> in the console
```

WebM plays directly in the desktop app (Chromium/WebView2) and Android — no HLS or
extra JS.

## Install the tools on the Orca server

```
# Debian/Ubuntu
sudo apt install ffmpeg hackrf rtl-sdr
# leandvb (lightweight DVB-S2 demod) — build from source (pabr/leansdr),
# or install gr-dvbs2rx for a GNU Radio flow.
```

`GET /api/video/status` reports what's present; the console's **Decode FTA video**
button is enabled only when `leandvb`/`gr-dvbs2rx` **and** `ffmpeg` are installed.

## Using it

1. Tune to a satellite-TV band (C 3.4–4.2, Ku 10.7–12.75, Ka 17.7–21.2 GHz) through
   your LNB's L-band IF.
2. The Live monitor shows the **SR** field and the Decode button.
3. Set the transponder symbol rate (MSym/s) — Orca's satellite profile estimates it
   from the occupied bandwidth — and click **Decode FTA video**.
4. If the service is encrypted, Orca names the CA system and refuses: identify-only.

The leandvb flags in `orca/dvb.py` (`build_pipeline`) are a starting template; a real
dish/LNB/MODCOD setup tunes sample rate, roll-off and gain. Receive-only throughout.
