"""Live-audio demodulation and band media mapping."""
import numpy as np

from orca import audio


def _fm_tone(rate, n, af=1000.0, dev=5000.0):
    t = np.arange(n) / rate
    phase = 2 * np.pi * dev * np.cumsum(np.sin(2 * np.pi * af * t)) / rate
    return np.exp(1j * phase).astype(np.complex64)


def _am_tone(rate, n, af=1000.0):
    t = np.arange(n) / rate
    return ((1 + 0.6 * np.sin(2 * np.pi * af * t))).astype(np.complex64)


def test_fm_demod_recovers_a_tone_in_range():
    rate = audio.MODE_RATE["nfm"]
    n = rate // 10
    a = audio.demodulate(_fm_tone(rate, n), rate, "nfm")
    assert a.dtype == np.float32
    factor = round(rate / audio.AUDIO_RATE)
    assert abs(len(a) - n // factor) <= 2                        # ~100 ms at 48 kHz
    assert np.max(np.abs(a)) <= 1.0 and np.max(np.abs(a)) > 0.1   # real audio, bounded


def test_am_demod_bounded():
    rate = audio.MODE_RATE["am"]
    a = audio.demodulate(_am_tone(rate, rate // 10), rate, "am")
    assert np.max(np.abs(a)) <= 1.0 and a.size > 0


def test_ssb_usb_lsb_differ():
    rate = audio.MODE_RATE["usb"]
    t = np.arange(rate // 10) / rate
    iq = np.exp(2j * np.pi * 1200 * t).astype(np.complex64)   # upper-side tone
    u = audio.demodulate(iq, rate, "usb")
    l = audio.demodulate(iq, rate, "lsb")
    assert u.size == l.size and u.size > 0
    # the product detector mirrors for LSB, so the two are not identical
    assert not np.allclose(u, l)


def test_pcm16_roundtrip_range():
    a = np.array([-1.0, 0.0, 0.5, 1.0], np.float32)
    pcm = audio.to_pcm16(a)
    vals = np.frombuffer(pcm, "<i2")
    assert vals[0] == -32767 and vals[-1] == 32767


def test_empty_iq_is_safe():
    assert audio.demodulate(np.zeros(2, np.complex64), 48000, "nfm").size == 0


def test_media_for_bands():
    assert audio.media_for(145e6)["audio"] is True          # 2 m FM voice
    assert "nfm" in audio.media_for(145e6)["modes"]
    assert audio.media_for(14.2e6)["audio"] is True          # HF SSB
    assert "usb" in audio.media_for(14.2e6)["modes"]
    ku = audio.media_for(11.785e9)                           # Ku satellite TV
    assert ku["video"] is True
    assert "free-to-air" in ku["note"] and "not descrambled" in ku["note"]
    assert audio.media_for(5.0e9)["video"] is False          # not a TV band
