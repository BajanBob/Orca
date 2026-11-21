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


# --- Transmission modes (how the carrier is coded) ----------------------------
# These are physical/transport-layer modes a monitoring receiver must recognise
# to lock and demux. Recognising and receiving them is ordinary DVB signal work.
TX_MODES = {
    "CCM": "Constant Coding & Modulation — one fixed MODCOD for the whole carrier.",
    "VCM": "Variable Coding & Modulation — MODCOD varies per stream/slice; robust "
           "streams and efficient streams share one carrier (common on feeds/DSNG).",
    "ACM": "Adaptive Coding & Modulation — MODCOD tracks link conditions in real time "
           "(VSAT/IP return links). Receiver must follow MODCOD per frame.",
    "MIS": "Multiple Input Stream (multistream) — several independent transport "
           "streams multiplexed on one carrier, separated by Input Stream Id (ISI).",
    "T2MI": "T2-MI — a DVB-T2 Modulator Interface stream carried over satellite "
            "(DVB-S2), feeding terrestrial transmitters; demux the T2-MI to reach "
            "the inner TS. Orca parses the T2-MI wrapper to list the carried streams.",
}


# --- Conditional-access systems: IDENTIFY ONLY, never descramble ---------------
# Public DVB CA_system_id registrations. Orca reads the CA descriptor to NAME the
# system and flag the service as encrypted. It does not hold keys for, roll keys
# for, or descramble any third-party CA system — including BISS, PowerVu, Conax
# and DigiCipher. That boundary is what keeps Orca lawful to build and sell.
CA_SYSTEMS = [
    ("BISS",            (0x2600, 0x2600), "BISS-1/BISS-E feed scrambling (named by user)"),
    ("PowerVu",         (0x0E00, 0x0EFF), "Cisco/Scientific-Atlanta PowerVu (named by user)"),
    ("Conax",           (0x0B00, 0x0BFF), "Conax CAS (named by user)"),
    ("DigiCipher 2",    (0x4A60, 0x4A63), "Motorola/GI DigiCipher II (named by user)"),
    ("DigiCipher 2",    (0x4AD0, 0x4AD1), "Motorola/GI DigiCipher II"),
    ("Nagravision",     (0x1800, 0x18FF), "Nagra / Media Cipher"),
    ("Irdeto",          (0x0600, 0x06FF), "Irdeto"),
    ("Viaccess",        (0x0500, 0x05FF), "Viaccess"),
    ("Videoguard/NDS",  (0x0900, 0x09FF), "NDS Videoguard"),
    ("Mediaguard/SECA", (0x0100, 0x01FF), "Mediaguard / SECA"),
    ("Cryptoworks",     (0x0D00, 0x0DFF), "Cryptoworks"),
]


def identify_ca(ca_system_id: int | None) -> dict:
    """Name the conditional-access system from its CA_system_id. Report only.

    Returns the system name, whether the service is encrypted, and the fixed
    action Orca takes. The action is always 'identify-only' for an encrypted
    service and 'decode' for free-to-air — there is no descramble path.
    """
    if ca_system_id is None:
        return {"encrypted": False, "ca_system": None, "ca_system_id": None,
                "action": "decode", "note": "No CA descriptor: free-to-air — demux and decode."}
    for name, (lo, hi), desc in CA_SYSTEMS:
        if lo <= ca_system_id <= hi:
            return {"encrypted": True, "ca_system": name,
                    "ca_system_id": f"0x{ca_system_id:04X}", "detail": desc,
                    "action": "identify-only",
                    "note": f"{name}-encrypted. Orca reports it; it does not descramble "
                            "conditional-access content."}
    return {"encrypted": True, "ca_system": "unknown CAS",
            "ca_system_id": f"0x{ca_system_id:04X}", "action": "identify-only",
            "note": "Scrambled by an unrecognised CA system — identified as encrypted, not decoded."}


def transport_profile(tx_mode: str = "CCM", streams: list[dict] | None = None,
                      t2mi: bool = False) -> dict:
    """Describe a carrier's transmission mode and per-stream CA status (report only).

    `streams` is a list of {isi, service, ca_system_id} as read from the stream
    headers / CA descriptors. Orca lists each stream, names any CA system, and
    decodes only the free-to-air ones.
    """
    mode = "T2MI" if t2mi else (tx_mode or "CCM").upper()
    out = {"tx_mode": mode, "tx_mode_desc": TX_MODES.get(mode, "unknown mode"),
           "multistream": mode == "MIS" or (streams is not None and len(streams) > 1),
           "streams": []}
    for s in (streams or []):
        ca = identify_ca(s.get("ca_system_id"))
        out["streams"].append({
            "isi": s.get("isi"), "service": s.get("service", ""),
            "encrypted": ca["encrypted"], "ca_system": ca["ca_system"],
            "action": ca["action"],
        })
    fta = [s for s in out["streams"] if not s["encrypted"]]
    enc = [s for s in out["streams"] if s["encrypted"]]
    out["summary"] = (f"{mode}: {len(out['streams'])} stream(s), "
                      f"{len(fta)} free-to-air (decodable), {len(enc)} encrypted (identify-only).")
    return out


def recommendations() -> dict:
    """What Orca's satellite decoder should support, and how."""
    return {
        "demodulation": {
            "standards": ["DVB-S", "DVB-S2", "DVB-S2X"],
            "modulations": ["QPSK", "8PSK", "16APSK", "32APSK", "64APSK", "128APSK", "256APSK"],
            "symbol_rate": "0.64-45 MSym/s",
            "rolloff": [0.35, 0.25, 0.20, 0.15, 0.10, 0.05],
            "tx_modes": ["CCM", "VCM", "ACM", "Multistream (MIS/ISI)", "T2-MI"],
            "how": "Use an open DVB-S2/S2X demodulator (gr-dvbs2rx or leandvb under GNU Radio) "
                   "fed from the SDR; identify MODCOD, symbol rate and roll-off automatically, "
                   "follow per-frame MODCOD for VCM/ACM, separate multistream by Input Stream Id, "
                   "and unwrap T2-MI to reach the inner transport stream.",
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
        "conditional_access": {
            "identify": ["BISS", "PowerVu", "Conax", "DigiCipher 2", "Nagravision",
                         "Irdeto", "Viaccess", "Videoguard/NDS", "Mediaguard", "Cryptoworks"],
            "how": "Read the CA descriptor / CA_system_id from the stream and report the system "
                   "name and that the service is encrypted.",
            "descramble": "never — no key store, no key 'auto-roll', no CA emulation for any "
                          "third-party system, including BISS, PowerVu, Conax and DigiCipher.",
        },
        "boundary": "Decode free-to-air only. Identify conditional-access (BISS, PowerVu, Conax, "
                    "DigiCipher, Nagra, Irdeto, Videoguard, etc.) but never descramble it. The "
                    "secure module's encryption is for the operator's OWN comms on licensed "
                    "non-amateur bands — it is not a CA/pay-TV descrambler.",
    }
