"""Direct HackRF receive source via libhackrf (ctypes) - no SoapySDR needed.

This exists so the frozen Windows build can use a HackRF: it loads hackrf.dll
(bundled next to the exe) through ctypes and streams RX IQ, instead of depending
on a separate SoapySDR install the PyInstaller bundle can't see.

Receive only. Orca never transmits; this module calls no TX function.

The pure IQ conversion (int8 interleaved -> complex64) is factored out as
`bytes_to_iq` so it can be unit-tested without the library or hardware. The ctypes
plumbing only runs when a real HackRF and hackrf.dll are present; everywhere else
Orca falls back to the simulator with a clear reason (see sdr.make_source).
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import numpy as np

from .bands import FRONTENDS
from .sdr import Source

_SUCCESS = 0


def bytes_to_iq(raw: bytes) -> np.ndarray:
    """HackRF signed-8-bit interleaved I,Q -> complex64 in ~[-1, 1]."""
    a = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0
    if a.size % 2:
        a = a[:-1]
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def _candidate_libs() -> list[str]:
    names = ["hackrf", "libhackrf"]
    base = getattr(sys, "_MEIPASS", None)
    here = [Path(base)] if base else []
    here.append(Path(__file__).resolve().parent)
    files = []
    for d in here:
        for fn in ("hackrf.dll", "libhackrf.dll", "libhackrf.so.0", "libhackrf.so", "libhackrf.dylib"):
            files.append(str(d / fn))
    return files + names


def _load_lib():
    import ctypes
    last = None
    for cand in _candidate_libs():
        try:
            return ctypes.CDLL(cand)
        except OSError as e:
            last = e
    raise OSError(f"libhackrf not found ({last})")


class _Transfer:
    pass  # placeholder; real struct built inside HackRFSource to keep ctypes local


class HackRFSource(Source):
    """Receive-only HackRF via libhackrf. Tunes and collects IQ per capture()."""
    key = "hackrf"

    def __init__(self, lna_gain: int = 16, vga_gain: int = 20, amp: int = 0,
                 max_rate: float = 10e6, device_index: int = 0):
        import ctypes
        self.ct = ctypes
        self.lib = _load_lib()
        self.max_rate = float(max_rate)
        self.device_label = "driver=hackrf (native)"
        self._lna, self._vga, self._amp = int(lna_gain), int(vga_gain), int(amp)
        self._rate = 0.0
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._want = 0
        self._done = threading.Event()

        class Transfer(ctypes.Structure):
            _fields_ = [("device", ctypes.c_void_p),
                        ("buffer", ctypes.POINTER(ctypes.c_uint8)),
                        ("buffer_length", ctypes.c_int),
                        ("valid_length", ctypes.c_int),
                        ("rx_ctx", ctypes.c_void_p),
                        ("tx_ctx", ctypes.c_void_p)]
        self._Transfer = Transfer
        self._CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(Transfer))

        self.lib.hackrf_init()
        self.dev = ctypes.c_void_p()
        if self.lib.hackrf_open(ctypes.byref(self.dev)) != _SUCCESS:
            raise OSError("hackrf_open failed (device not found or in use)")
        self._cb_ref = self._CB(self._on_samples)      # keep a reference alive

    # ------- RX callback: append IQ until we have enough -------
    def _on_samples(self, transfer_ptr):
        t = transfer_ptr.contents
        n = int(t.valid_length)
        raw = bytes(bytearray(self.ct.cast(
            t.buffer, self.ct.POINTER(self.ct.c_uint8 * n)).contents))
        iq = bytes_to_iq(raw)
        with self._lock:
            if self._want:
                self._chunks.append(iq)
                if sum(len(c) for c in self._chunks) >= self._want:
                    self._done.set()
        return 0

    def _read(self, tune_hz: float, rate: float, n: int) -> np.ndarray:
        ct = self.ct
        if rate != self._rate:
            self.lib.hackrf_set_sample_rate(self.dev, ct.c_double(float(rate)))
            self._rate = rate
        self.lib.hackrf_set_freq(self.dev, ct.c_uint64(int(tune_hz)))
        self.lib.hackrf_set_amp_enable(self.dev, ct.c_uint8(self._amp))
        self.lib.hackrf_set_lna_gain(self.dev, ct.c_uint32(self._lna))
        self.lib.hackrf_set_vga_gain(self.dev, ct.c_uint32(self._vga))
        with self._lock:
            self._chunks = []
            self._want = n + 262144           # drop the settling samples below
            self._done.clear()
        self.lib.hackrf_start_rx(self.dev, self._cb_ref, None)
        got = self._done.wait(timeout=3.0)
        self.lib.hackrf_stop_rx(self.dev)
        with self._lock:
            data = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.complex64)
            self._want = 0
        if not got or data.size < n + 262144:
            return data[-n:] if data.size >= n else data
        return data[262144:262144 + n]        # skip PLL settle, return n samples

    async def capture(self, rf_hz, rate, n, frontend="none"):
        fe = FRONTENDS[frontend]
        tune = rf_hz if fe.key == "none" else fe.to_if(rf_hz)
        iq = await asyncio.get_running_loop().run_in_executor(None, self._read, tune, rate, n)
        return np.conj(iq) if fe.inverted else iq

    def close(self):
        try:
            self.lib.hackrf_stop_rx(self.dev)
            self.lib.hackrf_close(self.dev)
            self.lib.hackrf_exit()
        except Exception:
            pass
