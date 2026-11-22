"""Direct RTL-SDR receive source via librtlsdr (ctypes) - no SoapySDR needed.

Lets the frozen Windows build use an RTL-SDR dongle (e.g. Nooelec NESDR SMArTee
XTR) by loading rtlsdr.dll through ctypes and reading IQ with the synchronous
rtlsdr_read_sync API - simpler and more robust than a callback stream.

Receive only. The pure cu8->complex64 conversion (cu8_to_iq) is unit-tested; the
ctypes path runs only with hardware + the library present, and Orca falls back to
the simulator with a clear reason otherwise (see sdr.make_source).

Coverage depends on the tuner (E4000: ~65 MHz-2.3 GHz; R820T2: ~25 MHz-1.75 GHz).
No HF without an upconverter. Max stable sample rate ~2.4 MS/s.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np

from .bands import FRONTENDS
from .sdr import Source


def cu8_to_iq(raw: bytes) -> np.ndarray:
    """RTL-SDR unsigned-8-bit interleaved I,Q -> complex64 in ~[-1, 1]."""
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    a = (a - 127.5) / 127.5
    if a.size % 2:
        a = a[:-1]
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def _candidate_libs() -> list[str]:
    files = []
    bases = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        bases.append(Path(mp))
    bases.append(Path(__file__).resolve().parent)
    for d in bases:
        for fn in ("rtlsdr.dll", "librtlsdr.dll", "librtlsdr.so.0", "librtlsdr.so", "librtlsdr.dylib"):
            files.append(str(d / fn))
    return files + ["rtlsdr", "librtlsdr"]


def _load_lib():
    import ctypes
    last = None
    for cand in _candidate_libs():
        try:
            return ctypes.CDLL(cand)
        except OSError as e:
            last = e
    raise OSError(f"librtlsdr not found ({last})")


class RtlSdrSource(Source):
    """Receive-only RTL-SDR via librtlsdr, synchronous reads."""
    key = "rtlsdr"
    max_rate = 2.4e6

    def __init__(self, gain_tenths_db: int | None = None, device_index: int = 0,
                 max_rate: float = 2.4e6):
        import ctypes
        self.ct = ctypes
        self.lib = _load_lib()
        self.max_rate = float(max_rate)
        self.device_label = "driver=rtlsdr (native)"
        self._rate = 0.0
        L = self.lib
        L.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        L.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        L.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        L.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
        L.rtlsdr_read_sync.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_int)]
        L.rtlsdr_close.argtypes = [ctypes.c_void_p]

        self.dev = ctypes.c_void_p()
        if L.rtlsdr_open(ctypes.byref(self.dev), int(device_index)) != 0:
            raise OSError("rtlsdr_open failed (device not found, or WinUSB driver not set via Zadig)")
        if gain_tenths_db is None:
            L.rtlsdr_set_tuner_gain_mode(self.dev, 0)        # 0 = automatic gain
        else:
            L.rtlsdr_set_tuner_gain_mode(self.dev, 1)
            L.rtlsdr_set_tuner_gain(self.dev, int(gain_tenths_db))

    def _read(self, tune_hz: float, rate: float, n: int) -> np.ndarray:
        ct = self.ct
        if rate != self._rate:
            self.lib.rtlsdr_set_sample_rate(self.dev, ct.c_uint32(int(rate)))
            self._rate = rate
        self.lib.rtlsdr_set_center_freq(self.dev, ct.c_uint32(int(tune_hz)))
        self.lib.rtlsdr_reset_buffer(self.dev)
        nbytes = 2 * n
        buf = (ct.c_ubyte * nbytes)()
        got = ct.c_int(0)
        # one throwaway read lets the tuner/AGC settle after retune
        self.lib.rtlsdr_read_sync(self.dev, buf, nbytes, ct.byref(got))
        got = ct.c_int(0)
        if self.lib.rtlsdr_read_sync(self.dev, buf, nbytes, ct.byref(got)) != 0:
            return np.zeros(0, np.complex64)
        return cu8_to_iq(bytes(buf[:got.value]))

    async def capture(self, rf_hz, rate, n, frontend="none"):
        fe = FRONTENDS[frontend]
        tune = rf_hz if fe.key == "none" else fe.to_if(rf_hz)
        rate = min(rate, self.max_rate)
        iq = await asyncio.get_running_loop().run_in_executor(None, self._read, tune, rate, n)
        return np.conj(iq) if fe.inverted else iq

    def close(self):
        try:
            self.lib.rtlsdr_close(self.dev)
        except Exception:
            pass
