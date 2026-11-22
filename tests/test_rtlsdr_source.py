"""Native RTL-SDR source: cu8 conversion (pure) and graceful fallback."""
import numpy as np

from orca import sdr
from orca.rtlsdr_native import cu8_to_iq


def test_cu8_to_iq_centers_and_scales():
    raw = bytes([128, 128, 255, 0, 0, 255])     # mid, max-I/min-Q, min-I/max-Q
    iq = cu8_to_iq(raw)
    assert iq.dtype == np.complex64 and iq.size == 3
    assert abs(iq[0].real) < 0.01 and abs(iq[0].imag) < 0.01      # 128 -> ~0
    assert iq[1].real > 0.9 and iq[1].imag < -0.9
    assert iq[2].real < -0.9 and iq[2].imag > 0.9


def test_cu8_drops_odd_trailing_byte():
    assert cu8_to_iq(bytes([200, 50, 10])).size == 1


def test_make_source_rtlsdr_falls_back_without_lib():
    src = sdr.make_source({"kind": "rtlsdr"})
    assert src.key == "sim"
    assert "RTL-SDR" in getattr(src, "fallback_note", "")


def test_make_source_rtlsdr_respects_gain_without_crashing():
    # gain given but no hardware -> still a clean sim fallback, no exception
    src = sdr.make_source({"kind": "rtlsdr", "gain_db": 40})
    assert src.key == "sim"


def test_make_source_auto_falls_back_to_sim_without_hardware():
    # auto probes rtlsdr then hackrf; with neither present it simulates, cleanly.
    src = sdr.make_source({"kind": "auto", "scenarios": ["gnss_jam"]})
    assert src.key == "sim"
    assert "No SDR detected" in getattr(src, "fallback_note", "")
    assert "gnss_jam" in src.scenarios          # scenarios still load in fallback
