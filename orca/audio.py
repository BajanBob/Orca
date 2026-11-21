"""Live audio from the tuned signal: demodulate IQ to PCM for the console player.

Receive-only. Orca demodulates the narrowband signal it is already tuned to and
streams the audio to the browser; it never transmits. The demodulators are plain
NumPy so they run anywhere the simulator does.

Which bands carry audio/video is advisory (media_for): audio for voice/data
modes across HF/VHF/UHF and broadcast; video only for the satellite-TV bands,
and there only free-to-air (the DVB-S2 -> MPEG decode pipeline is external;
conditional-access is identified, never descrambled).
"""
from __future__ import annotations

import numpy as np

AUDIO_RATE = 48000

# narrowband capture rates per mode (Hz) — small spans, integer-decimated to 48k
MODE_RATE = {
    "nfm": 48000 * 5,     # 240 kHz -> FM discriminator
    "wfm": 48000 * 5,     # broadcast FM (mono)
    "am":  48000 * 4,     # 192 kHz
    "usb": 48000 * 2,     # 96 kHz SSB
    "lsb": 48000 * 2,
    "cw":  48000 * 2,
}
MODES = tuple(MODE_RATE)


def _decimate(x: np.ndarray, factor: int) -> np.ndarray:
    """Anti-aliased integer decimation with a short windowed-sinc low-pass."""
    if factor <= 1:
        return x
    taps = 8 * factor | 1
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(n / factor) * np.hamming(taps)
    h /= h.sum()
    y = np.convolve(x, h, mode="same")
    return y[::factor]


def demodulate(iq: np.ndarray, rate: float, mode: str = "nfm",
               audio_rate: int = AUDIO_RATE) -> np.ndarray:
    """Demodulate complex baseband IQ to float32 audio in [-1, 1]."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 4:
        return np.zeros(0, np.float32)
    mode = (mode or "nfm").lower()

    if mode in ("nfm", "wfm"):
        d = iq[1:] * np.conj(iq[:-1])
        a = np.angle(d).astype(np.float32)            # frequency discriminator
    elif mode == "am":
        a = np.abs(iq).astype(np.float32)
        a = a - np.mean(a)                            # strip the carrier (DC)
    elif mode in ("usb", "lsb"):
        # single-sideband: keep one half of the spectrum, then take the real part
        spec = np.fft.fft(iq)
        fr = np.fft.fftfreq(iq.size)
        spec[fr < 0] = 0 if mode == "usb" else spec[fr < 0]
        spec[fr > 0] = 0 if mode == "lsb" else spec[fr > 0]
        a = (np.real(np.fft.ifft(spec)) * 2).astype(np.float32)
    elif mode == "cw":
        t = np.arange(iq.size) / rate
        bfo = np.exp(2j * np.pi * 600.0 * t).astype(np.complex64)   # 600 Hz tone
        a = np.real(iq * bfo).astype(np.float32)
    else:
        a = np.real(iq).astype(np.float32)

    factor = max(1, int(round(rate / audio_rate)))
    a = _decimate(a, factor)
    # de-emphasis-ish smoothing for FM, and a gentle normalise
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    if peak > 1e-9:
        a = a / peak * 0.9
    return a.astype(np.float32)


def to_pcm16(audio: np.ndarray) -> bytes:
    """float32 [-1,1] -> little-endian int16 PCM bytes."""
    a = np.clip(audio, -1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


# --------------------------------------------------------------- band media map
def media_for(freq_hz: float) -> dict:
    """Advisory: does this frequency carry audio and/or (FTA) video, and how."""
    f = float(freq_hz)
    audio, video, modes, note = False, False, [], ""
    if 148e6 <= f <= 174e6 or 420e6 <= f <= 470e6:
        audio, modes, note = True, ["nfm"], "VHF/UHF FM voice"
    elif 118e6 <= f < 137e6:
        audio, modes, note = True, ["am"], "Airband AM voice"
    elif 530e3 <= f <= 1.71e6:
        audio, modes, note = True, ["am"], "AM broadcast"
    elif 87.5e6 <= f <= 108e6:
        audio, modes, note = True, ["wfm"], "FM broadcast (mono)"
    elif f < 30e6:
        audio, modes, note = True, ["usb", "lsb", "am", "cw"], "HF: SSB/AM/CW"
    elif 30e6 <= f < 148e6:
        audio, modes, note = True, ["nfm", "usb"], "VHF low/6m/2m"
    # Satellite-TV downlink bands: free-to-air video is decodable (pipeline external)
    if (3.4e9 <= f <= 4.2e9) or (10.7e9 <= f <= 12.75e9) or (17.7e9 <= f <= 21.2e9):
        video, note = True, (note + "; satellite TV: free-to-air video decodable, "
                             "conditional-access identified not descrambled").lstrip("; ")
    return {"audio": audio, "video": video, "modes": modes or (["nfm"] if audio else []),
            "note": note}
