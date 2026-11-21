"""Free-to-air DVB-S2 video decode: SDR IQ -> demod -> MPEG-TS -> WebM for the browser.

This drives standard open tools; it bundles none of them:

  SDR capture (hackrf_transfer / rtl_sdr, or SoapySDR) -> DVB-S2 demodulator
  (leandvb or gr-dvbs2rx) -> MPEG transport stream -> FFmpeg -> live WebM
  (VP8/Opus) streamed to a <video> element in the console.

Hard boundary, identical to the rest of Orca:

  * RECEIVE ONLY. Nothing here transmits.
  * FREE-TO-AIR ONLY. start() refuses any service carrying a conditional-access
    system (BISS, PowerVu, Conax, DigiCipher, Nagra, ...). Orca identifies CA via
    satcom.identify_ca and stops there - it never descrambles, holds keys, or
    emulates a CA system. A caller must not pass an encrypted service.

The pipeline command is built by a pure function (build_pipeline) so it is
inspectable and unit-testable; the decoder only spawns a process when start() is
called and the tools exist. Flags are a sensible starting template a real
deployment tunes for its LNB/dish and MODCOD.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field

from . import satcom

# Tools we orchestrate (never bundle).
DEMOD_TOOLS = ("leandvb", "dvbs2-rx")          # leandvb, or gr-dvbs2rx's CLI
FFMPEG = "ffmpeg"
SDR_CAPTURE = {"hackrf": "hackrf_transfer", "rtlsdr": "rtl_sdr"}


def tools_status() -> dict:
    """Which parts of the decode chain are installed on this host."""
    demod = next((t for t in DEMOD_TOOLS if shutil.which(t)), None)
    caps = {k: shutil.which(v) is not None for k, v in SDR_CAPTURE.items()}
    return {
        "demod": demod, "demod_ok": demod is not None,
        "ffmpeg": shutil.which(FFMPEG) is not None,
        "sdr_capture": caps,
        "ready": demod is not None and shutil.which(FFMPEG) is not None,
    }


def _sr_hz(symrate) -> int:
    sr = float(symrate)
    return int(sr * 1e6 if sr < 1e4 else sr)        # accept MSym/s or Sym/s


def build_pipeline(freq_hz: float, symrate, sdr: str = "hackrf",
                   samp_rate: float = 2.4e6, demod: str = "leandvb") -> dict:
    """Build the three-stage shell pipeline (pure; no side effects).

    Returns {"source", "demod", "mux"} command lists joined by pipes by the
    decoder. The mux stage emits live WebM (VP8/Opus) on stdout, which a browser
    <video> plays directly - no HLS or extra JS needed in a Chromium webview.
    """
    sr = _sr_hz(symrate)
    f = int(freq_hz)
    srate = int(samp_rate)
    if sdr == "rtlsdr":
        source = ["rtl_sdr", "-f", str(f), "-s", str(srate), "-"]
        fmt = ["--u8"]                              # rtl_sdr: unsigned 8-bit IQ
    else:
        source = ["hackrf_transfer", "-r", "-", "-f", str(f), "-s", str(srate), "-a", "1"]
        fmt = ["--s8"]                              # hackrf: signed 8-bit IQ
    if demod == "leandvb":
        dem = ["leandvb", "--standard", "DVB-S2", "-f", str(srate), "--sr", str(sr),
               "--sampler", "rrc", "--roll-off", "0.20", *fmt, "--ts", "-"]
    else:
        dem = ["dvbs2-rx", "--freq", str(f), "--sym-rate", str(sr), "--out-fd", "1"]
    mux = [FFMPEG, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
           "-i", "pipe:0",
           "-map", "0:v:0?", "-map", "0:a:0?",
           "-c:v", "libvpx", "-b:v", "1500k", "-deadline", "realtime", "-cpu-used", "5",
           "-c:a", "libopus", "-b:a", "96k",
           "-f", "webm", "-cluster_time_limit", "500", "pipe:1"]
    return {"source": source, "demod": dem, "mux": mux}


@dataclass
class FtaDecoder:
    """Runs one free-to-air decode at a time and yields live WebM bytes."""
    sdr: str = "hackrf"
    samp_rate: float = 2.4e6
    _procs: list = field(default_factory=list)
    running: bool = False
    info: dict = field(default_factory=dict)

    def gate(self, ca_system_id=None) -> dict:
        """Check the FTA boundary and tool availability before starting."""
        ca = satcom.identify_ca(ca_system_id)
        if ca["encrypted"]:
            return {"ok": False, "reason": "conditional-access",
                    "message": f"{ca['ca_system']} service: Orca identifies it but does not "
                               "descramble conditional-access content. Free-to-air only."}
        t = tools_status()
        if not t["ready"]:
            missing = []
            if not t["demod_ok"]:
                missing.append("a DVB-S2 demodulator (leandvb or gr-dvbs2rx)")
            if not t["ffmpeg"]:
                missing.append("ffmpeg")
            return {"ok": False, "reason": "tools-missing",
                    "message": "Install " + " and ".join(missing) + " on the Orca server to "
                               "decode free-to-air video. The carrier is still identified and "
                               "its codecs profiled without them."}
        return {"ok": True, "reason": "ready", "message": "Free-to-air decode ready."}

    async def start(self, freq_hz: float, symrate, ca_system_id=None, demod=None) -> dict:
        g = self.gate(ca_system_id)
        if not g["ok"]:
            return g
        await self.stop()
        t = tools_status()
        demod = demod or t["demod"] or "leandvb"
        p = build_pipeline(freq_hz, symrate, self.sdr, self.samp_rate, demod)
        try:
            src = await asyncio.create_subprocess_exec(
                *p["source"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            dem = await asyncio.create_subprocess_exec(
                *p["demod"], stdin=src.stdout, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            mux = await asyncio.create_subprocess_exec(
                *p["mux"], stdin=dem.stdout, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except Exception as e:
            await self.stop()
            return {"ok": False, "reason": "spawn-failed", "message": f"Could not start decode: {e}"}
        self._procs = [src, dem, mux]
        self.running = True
        self.info = {"freq_hz": freq_hz, "symrate": symrate, "sdr": self.sdr, "demod": demod}
        return {"ok": True, "reason": "started", "message": "Decoding free-to-air video."}

    async def frames(self):
        """Async generator of live WebM bytes for a StreamingResponse."""
        if not self.running or not self._procs:
            return
        mux = self._procs[-1]
        try:
            while self.running:
                chunk = await mux.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            await self.stop()

    async def stop(self) -> None:
        self.running = False
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        self._procs = []
