"""Native HackRF source: IQ conversion (pure) and graceful fallback selection."""
import numpy as np

from orca import sdr
from orca.hackrf_native import bytes_to_iq


def test_bytes_to_iq_converts_interleaved_int8():
    raw = bytes([127, 0, 0, 127, 128, 128])     # (I,Q) pairs incl. a negative
    iq = bytes_to_iq(raw)
    assert iq.dtype == np.complex64 and iq.size == 3
    assert abs(iq[0].real - 127/128) < 1e-6 and abs(iq[0].imag) < 1e-6
    assert abs(iq[1].imag - 127/128) < 1e-6
    # 128 as signed int8 is -128 -> -1.0
    assert iq[2].real < -0.9 and iq[2].imag < -0.9


def test_bytes_to_iq_drops_odd_trailing_sample():
    assert bytes_to_iq(bytes([1, 2, 3])).size == 1


def test_make_source_hackrf_falls_back_to_sim_without_lib():
    # No hackrf.dll in CI/sandbox -> must fall back to the simulator, not crash.
    src = sdr.make_source({"kind": "hackrf"})
    assert src.key == "sim"
    assert "HackRF" in getattr(src, "fallback_note", "")


def test_make_source_soapy_falls_back_to_sim_without_lib():
    src = sdr.make_source({"kind": "soapy", "args": "driver=hackrf"})
    assert src.key == "sim"
    assert "SoapySDR" in getattr(src, "fallback_note", "")


def test_make_source_sim_has_no_fallback_note():
    src = sdr.make_source({"kind": "sim"})
    assert src.key == "sim" and getattr(src, "fallback_note", "") == ""
    assert src.device_label == "simulator"
