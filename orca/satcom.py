"""Satellite downlink intelligence: identify DVB signals and the codecs they carry.

Orca characterises a satellite carrier it detects — which band, the likely DVB
standard, the symbol rate (from the occupied bandwidth), the probable modulation,
and the video/audio codecs the service uses. For a free-to-air (unencrypted)
carrier it can go on to demux the MPEG transport stream and confirm the codecs.

Boundary (unchanged from the rest of Orca): Orca does not descramble
conditional-access ("pay-TV") content. It identifies and, for free-to-air
signals, decodes; it does not defeat encryption or a conditional-access system.

Reference: DVB-S2 (ETSI EN 302 307-1) and DVB-S2X (EN 302 307-2); symbol rate =
bandwidth / (1 + roll-off), with roll-off 0.35/0.25/0.20 for DVB-S2 and
0.20/0.15/0.10/0.05 for DVB-S2X.
"""
from __future__ import annotations

from dataclasses import dataclass

GHz, MHz = 1e9, 1e6

# --- Satellite bands and the services on them (downlink ranges, Hz) -----------
@dataclass(frozen=True)
class SatBand:
    key: str
    name: str
    dl_lo: float
    dl_hi: float
    ul_lo: float
    ul_hi: float
    services: str
    notes: str


SAT_BANDS = [
    SatBand("l", "L band", 1.0 * GHz, 2.0 * GHz, 1.0 * GHz, 2.0 * GHz,
            "Mobile satellite (Inmarsat, Iridium, Thuraya), GNSS, DVB-SH; also the LNB "
            "IF output (950-2150 MHz) that every DVB receiver actually tunes",
            "C/Ku/Ka downlinks are block-downconverted by the LNB to this L-band IF. "
            "Orca tunes the IF through the same chain."),
    SatBand("s", "S band", 2.0 * GHz, 3.4 * GHz, 2.0 * GHz, 3.4 * GHz,
            "Satellite radio (SiriusXM ~2.32-2.345 GHz), mobile satellite, DVB-SH, TT&C",
            "Shared with Wi-Fi and radar; satellite use is narrow sub-bands."),
    SatBand("c", "C band", 3.4 * GHz, 4.2 * GHz, 5.85 * GHz, 6.725 * GHz,
            "Satellite TV (DTH and broadcast feeds), VSAT, DSNG — rain-resilient, "
            "favoured in the tropics and for reliability",
            "Extended C-band from 3.4 GHz. Large dishes; robust in heavy rain."),
    SatBand("x", "X band", 7.25 * GHz, 7.75 * GHz, 7.9 * GHz, 8.4 * GHz,
            "Government and military SATCOM, some Earth-observation downlink",
            "Mostly restricted/defence use; often encrypted (not decoded by Orca)."),
    SatBand("ku", "Ku band", 10.7 * GHz, 12.75 * GHz, 13.75 * GHz, 14.5 * GHz,
            "Direct-to-home satellite TV, VSAT, DSNG news gathering, Starlink user "
            "downlink (10.7-12.7 GHz)",
            "The main consumer TV band. Smaller dishes than C; rain-fade sensitive."),
    SatBand("ka", "Ka band", 17.7 * GHz, 21.2 * GHz, 27.5 * GHz, 31.0 * GHz,
            "High-throughput broadband (Viasat, Jupiter, OneWeb), Starlink gateways, "
            "military SATCOM",
            "Mostly IP/data (DVB-S2X), not broadcast TV; strong rain fade."),
]
SAT_BY_KEY = {b.key: b for b in SAT_BANDS}

# --- Codecs (the user's reference, aligned to standards) ----------------------
VIDEO_CODECS = [
    {"name": "MPEG-2", "std": "H.262", "use": "Legacy SD and some HD broadcasts",
     "note": "Still common on older SD services; being retired."},
    {"name": "MPEG-4 AVC (H.264)", "std": "H.264", "use": "HD broadcasting",
     "note": "The HD workhorse; widely deployed."},
    {"name": "HEVC (H.265)", "std": "H.265", "use": "4K / UHDTV",
     "note": "~30% better compression than H.264; broadcasters migrating here for UHD."},
    {"name": "AV1 / VVC (H.266)", "std": "AV1 / H.266", "use": "Emerging UHD / IP",
     "note": "Next generation; appearing on IP/OTT and some new satellite IP services."},
]
AUDIO_CODECS = [
    {"name": "MPEG-1 Layer 2 (MP2)", "use": "Very common legacy broadcast audio"},
    {"name": "MPEG-2 Layer 3 (MP3)", "use": "Occasional broadcast audio"},
    {"name": "Dolby Digital (AC-3)", "use": "Surround for HD services"},
    {"name": "Dolby Digital Plus (E-AC-3)", "use": "Efficient surround for HD/UHD"},
    {"name": "AAC-LC", "use": "Efficient stereo/surround"},
    {"name": "HE-AAC v1/v2", "use": "Low-bitrate audio"},
    {"name": "xHE-AAC", "use": "Adaptive-bitrate audio (newer receivers)"},
    {"name": "AAC-ELD", "use": "Low-delay contribution links"},
    {"name": "Opus", "use": "Low-latency / IP contribution"},
    {"name": "Enhanced aptX", "use": "Professional contribution audio"},
]

# --- DVB physical layer -------------------------------------------------------
DVB_STANDARDS = {
    "DVB-S": {"mods": ["QPSK"], "rolloffs": [0.35], "note": "First generation; QPSK only."},
    "DVB-S2": {"mods": ["QPSK", "8PSK", "16APSK", "32APSK"], "rolloffs": [0.35, 0.25, 0.20],
               "note": "Second generation; ACM, LDPC+BCH FEC."},
    "DVB-S2X": {"mods": ["QPSK", "8PSK", "16APSK", "32APSK", "64APSK", "128APSK", "256APSK"],
                "rolloffs": [0.20, 0.15, 0.10, 0.05],
                "note": "Extensions; finer MODCODs, low roll-off, VLSNR down to -10 dB."},
}
SYMBOL_RATE_MIN = 0.64e6        # typical decoder range 0.64-45 MSym/s
SYMBOL_RATE_MAX = 45e6


def band_of(freq_hz: float) -> SatBand | None:
    for b in SAT_BANDS:
        if b.dl_lo <= freq_hz <= b.dl_hi:
            return b
    return None


def symbol_rate_from_bw(bw_hz: float, rolloff: float = 0.20) -> float:
    """Symbol rate = occupied bandwidth / (1 + roll-off)."""
    return bw_hz / (1 + rolloff)


def bw_from_symbol_rate(sr: float, rolloff: float = 0.20) -> float:
    return sr * (1 + rolloff)


@dataclass
class SatProfile:
    band: str
    service: str
    sr_est_msps: float
    sr_range_msps: tuple[float, float]
    likely_standard: str
    modulations: list[str]
    video_codecs: list[str]
    audio_codecs: list[str]
    plausible: bool
    note: str

    def to_dict(self):
        d = dict(self.__dict__)
        d["sr_range_msps"] = list(self.sr_range_msps)
        return d


def profile(freq_hz: float, bw_hz: float) -> SatProfile | None:
    """Characterise a detected carrier as a probable satellite downlink."""
    b = band_of(freq_hz)
    if b is None:
        return None
    # symbol-rate estimate across the plausible roll-off span
    sr_hi = symbol_rate_from_bw(bw_hz, 0.05)
    sr_lo = symbol_rate_from_bw(bw_hz, 0.35)
    sr_mid = symbol_rate_from_bw(bw_hz, 0.20)
    plausible = SYMBOL_RATE_MIN <= sr_mid <= SYMBOL_RATE_MAX
    # standard guess: wide carriers are usually S2/S2X; very wide (HTS) S2X
    if sr_mid > 30e6:
        std = "DVB-S2X"
    elif sr_mid > 3e6:
        std = "DVB-S2"
    else:
        std = "DVB-S2 (or narrowband S2X/VLSNR)"
    mods = DVB_STANDARDS["DVB-S2X" if "S2X" in std else "DVB-S2"]["mods"]
    # codecs are service-driven, not band-driven; list the realistic set
    if b.key in ("ku", "c"):
        vid = ["MPEG-2 (legacy SD)", "H.264 (HD)", "HEVC (UHD/4K)"]
        aud = ["MPEG-1 L2", "AC-3", "E-AC-3", "AAC/HE-AAC"]
    elif b.key == "ka":
        vid = ["H.264", "HEVC", "AV1 (IP services)"]
        aud = ["AAC/HE-AAC", "Opus", "E-AC-3"]
    elif b.key in ("l", "s"):
        vid = ["H.264 (DVB-SH/mobile)", "HEVC"]
        aud = ["HE-AAC", "AAC-LC"]
    else:
        vid = ["service-dependent (often encrypted)"]
        aud = ["service-dependent"]
    note = ("Free-to-air carriers can be demuxed to confirm codecs; conditional-access "
            "(pay-TV) is identified but not descrambled.")
    if b.key == "x":
        note = "Government/military band: typically encrypted; Orca identifies, does not decode."
    return SatProfile(b.name, b.services.split(",")[0], round(sr_mid / 1e6, 2),
                      (round(sr_lo / 1e6, 2), round(sr_hi / 1e6, 2)), std, mods, vid, aud,
                      plausible, note)


def recommendations() -> dict:
    """What Orca's satellite decoder should support, and how."""
    return {
        "demodulation": {
            "standards": ["DVB-S", "DVB-S2", "DVB-S2X"],
            "modulations": ["QPSK", "8PSK", "16APSK", "32APSK", "64APSK", "128APSK", "256APSK"],
            "symbol_rate": "0.64-45 MSym/s",
            "rolloff": [0.35, 0.25, 0.20, 0.15, 0.10, 0.05],
            "how": "Use an open DVB-S2/S2X demodulator (gr-dvbs2rx or leandvb under GNU Radio) "
                   "fed from the SDR; identify MODCOD, symbol rate and roll-off automatically.",
        },
        "video_decode": {
            "support": ["MPEG-2", "H.264/AVC", "HEVC/H.265"],
            "prioritise": "HEVC — ~30% better than AVC; where UHD/4K is going. MPEG-2 only for legacy SD.",
            "emerging": ["AV1", "VVC/H.266"],
            "how": "Demux the MPEG transport stream (TSDuck) and probe/decode with FFmpeg; "
                   "both are open and cover all three codecs.",
        },
        "audio_decode": {
            "support": ["MPEG-1 L2", "AC-3", "E-AC-3 (DD+)", "AAC-LC", "HE-AAC v1/v2",
                        "xHE-AAC", "AAC-ELD", "Opus", "Enhanced aptX"],
            "how": "FFmpeg covers all but Enhanced aptX; add aptX only if a professional "
                   "contribution feed requires it.",
        },
        "rf_chain": {
            "bands": ["C", "Ku", "Ka", "L (IF)", "S", "X"],
            "how": "An LNB/BDC block-downconverts C/Ku/Ka to the L-band IF (950-2150 MHz), which "
                   "the SDR samples — the same downconverter chain Orca already plans in bands.py.",
        },
        "boundary": "Decode free-to-air only. Identify conditional-access (Nagra, Irdeto, "
                    "Videoguard, BISS, etc.) but never descramble it; that keeps Orca lawful to sell.",
    }
