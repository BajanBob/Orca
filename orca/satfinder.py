"""Satellite finder: look-angles, low-SNR carrier detection, and TS identification.

For aiming a dish/LNB at a geostationary satellite:
  * look_angle(lat, lon, sat_lon) -> true azimuth, elevation, LNB skew (vector math).
  * SAT_CATALOG + identify_satellite(sat_lon) -> nearest named satellite(s).
  * cn_estimate(iq, rate) -> carrier-to-noise in dB and a 0..1 strength for the
    alignment meter/beep. This detects carrier PRESENCE and strength far below the
    demod lock threshold - that is how a finder peaks a dish - without decoding
    anything.
  * parse_ts(ts_bytes) -> services with names, video/audio PIDs, and the network
    name + orbital position, for automatic PID/VID/name identification once a
    free-to-air lock is achieved. Scrambled services are listed as encrypted and
    never descrambled (the CA boundary holds).

Bands: works on whatever the LNB block-downconverts to the L-band IF, so L / C /
Ku / Ka are all supported through the same chain. Receive-only throughout.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

R_EARTH = 6378.137          # km
R_GEO = 42164.0             # km, geostationary radius


# ----------------------------------------------------------------- look angles
def look_angle(lat_deg: float, lon_deg: float, sat_lon_deg: float) -> dict:
    """True azimuth, elevation and LNB polarisation skew for a geo satellite.

    Vector (ECEF->ENU) method, correct in both hemispheres. Longitudes are east-
    positive (west negative). Elevation below 0 means the satellite is below the
    horizon (not visible).
    """
    phi = math.radians(lat_deg)
    le = math.radians(lon_deg)
    ls = math.radians(sat_lon_deg)
    # ECEF: x->0 lon, y->90E, z->north
    sat = (R_GEO * math.cos(ls), R_GEO * math.sin(ls), 0.0)
    obs = (R_EARTH * math.cos(phi) * math.cos(le),
           R_EARTH * math.cos(phi) * math.sin(le),
           R_EARTH * math.sin(phi))
    d = (sat[0] - obs[0], sat[1] - obs[1], sat[2] - obs[2])
    # ENU basis at the observer
    e = (-math.sin(le), math.cos(le), 0.0)
    n = (-math.sin(phi) * math.cos(le), -math.sin(phi) * math.sin(le), math.cos(phi))
    u = (math.cos(phi) * math.cos(le), math.cos(phi) * math.sin(le), math.sin(phi))
    dE = d[0] * e[0] + d[1] * e[1] + d[2] * e[2]
    dN = d[0] * n[0] + d[1] * n[1] + d[2] * n[2]
    dU = d[0] * u[0] + d[1] * u[1] + d[2] * u[2]
    az = (math.degrees(math.atan2(dE, dN))) % 360.0
    el = math.degrees(math.atan2(dU, math.hypot(dE, dN)))
    # LNB polarisation skew (approx) for linear-pol dishes
    dl = math.radians(sat_lon_deg - lon_deg)
    skew = math.degrees(math.atan2(math.sin(dl), math.tan(phi))) if lat_deg else 0.0
    return {"azimuth_deg": round(az, 1), "elevation_deg": round(el, 1),
            "skew_deg": round(skew, 1), "visible": el > 0}


# --------------------------------------------------------------- satellite catalog
@dataclass(frozen=True)
class Sat:
    lon: float          # orbital position, deg East (west negative)
    name: str
    bands: str


# Representative global set, Americas arc first (highest elevation from the
# Caribbean); editable/extendable per deployment.
SAT_CATALOG = [
    Sat(-30.0, "Hispasat 30W", "Ku/C"), Sat(-43.0, "Intelsat 11 (43W)", "C/Ku"),
    Sat(-45.0, "Intelsat 14 (45W)", "C/Ku"), Sat(-50.0, "Intelsat 1R/Star One (50W)", "C/Ku"),
    Sat(-53.0, "Intelsat 23 (53W)", "C/Ku"), Sat(-55.5, "Intelsat 34 (55.5W)", "C/Ku"),
    Sat(-58.0, "Intelsat 21 (58W)", "C/Ku"), Sat(-61.0, "Amazonas/Star One (61W)", "C/Ku"),
    Sat(-63.0, "Telstar 14 (63W)", "C/Ku"), Sat(-65.0, "Star One C1 (65W)", "C/Ku"),
    Sat(-67.0, "Intelsat 9 (67W)", "C/Ku"), Sat(-70.0, "Star One C2 (70W)", "C/Ku"),
    Sat(-87.0, "SES-2 (87W)", "C/Ku"), Sat(-101.0, "SES-3/Galaxy (101W)", "C/Ku"),
    Sat(-119.0, "Galaxy 23 (119W)", "Ku"),
    Sat(-0.8, "Thor/Intelsat (0.8W)", "Ku"), Sat(7.0, "Eutelsat 7 (7E)", "Ku/C"),
    Sat(9.0, "Eutelsat 9B (9E)", "Ku"), Sat(13.0, "Eutelsat Hot Bird (13E)", "Ku"),
    Sat(19.2, "SES Astra 1 (19.2E)", "Ku"), Sat(23.5, "SES Astra 3 (23.5E)", "Ku"),
    Sat(28.2, "SES Astra 2 (28.2E)", "Ku"),
    Sat(83.0, "Insat/GSAT (83E)", "C/Ku"), Sat(90.0, "Yamal (90E)", "C/Ku"),
    Sat(100.5, "AsiaSat 5 (100.5E)", "C/Ku"), Sat(108.2, "Telkom/SES-7 (108E)", "C/Ku"),
]


def identify_satellite(sat_lon_deg: float, tol: float = 1.0) -> list[dict]:
    """Nearest catalogued satellites to an orbital position (deg East)."""
    hits = sorted(SAT_CATALOG, key=lambda s: abs(s.lon - sat_lon_deg))
    return [{"lon": s.lon, "name": s.name, "bands": s.bands,
             "delta_deg": round(abs(s.lon - sat_lon_deg), 2)}
            for s in hits if abs(s.lon - sat_lon_deg) <= tol] or \
           [{"lon": hits[0].lon, "name": hits[0].name, "bands": hits[0].bands,
             "delta_deg": round(abs(hits[0].lon - sat_lon_deg), 2)}]


# --------------------------------------------------- low-SNR carrier detection
def cn_estimate(iq: np.ndarray, rate: float) -> dict:
    """Carrier-to-noise estimate and 0..1 strength for the finder meter/beep.

    Detects a carrier's presence and relative strength from the power spectrum,
    well below the demod lock threshold (presence, not content). strength maps a
    roughly -5..+20 dB C/N onto 0..1 for the alignment beep.
    """
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 64:
        return {"cn_db": -99.0, "strength": 0.0, "locked_hint": False}
    win = np.hanning(iq.size).astype(np.float32)
    psd = np.abs(np.fft.fftshift(np.fft.fft(iq * win))) ** 2
    psd = psd / psd.size
    noise = np.median(psd)                       # robust noise floor
    peak = float(np.max(psd))
    inband = float(np.mean(np.sort(psd)[-max(1, psd.size // 50):]))   # top 2% avg
    cn = 10.0 * math.log10(max(inband - noise, 1e-12) / max(noise, 1e-12))
    strength = max(0.0, min(1.0, (cn + 5.0) / 25.0))
    return {"cn_db": round(cn, 1), "strength": round(strength, 3),
            "locked_hint": cn > 3.0, "peak_db": round(10 * math.log10(max(peak, 1e-12)), 1)}


# --------------------------------------------- MPEG-TS identification (FTA)
_SYNC = 0x47


def _sections(ts: bytes, want_pid: int) -> list[bytes]:
    """Reassemble PSI sections for one PID from a TS byte stream (simple path)."""
    buf = bytearray()
    out = []
    started = False
    for i in range(0, len(ts) - 187, 188):
        if ts[i] != _SYNC:
            continue
        pid = ((ts[i + 1] & 0x1F) << 8) | ts[i + 2]
        if pid != want_pid:
            continue
        pusi = ts[i + 1] & 0x40
        afc = (ts[i + 3] >> 4) & 0x3
        off = i + 4
        if afc & 0x2:                            # skip adaptation field
            off += 1 + ts[i + 4]
        payload = ts[off:i + 188]
        if pusi:
            if started and buf:
                out.append(bytes(buf))
            ptr = payload[0] if payload else 0
            buf = bytearray(payload[1 + ptr:])
            started = True
        elif started:
            buf += payload
    if started and buf:
        out.append(bytes(buf))
    return out


_VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x27}      # MPEG-1/2, MPEG-4, H.264, HEVC
_AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}      # MP2, AAC, AC-3, E-AC-3


def parse_ts(ts: bytes) -> dict:
    """Identify services, PIDs (incl. video), names and orbital position from a TS."""
    result = {"services": [], "network_name": "", "orbital_pos": None}
    # PAT (PID 0): program_number -> PMT PID
    pmt_pids = {}
    for sec in _sections(ts, 0x00):
        if not sec or sec[0] != 0x00:
            continue
        slen = ((sec[1] & 0x0F) << 8) | sec[2]
        body = sec[8:3 + slen - 4]
        for j in range(0, len(body) - 3, 4):
            prog = (body[j] << 8) | body[j + 1]
            pid = ((body[j + 2] & 0x1F) << 8) | body[j + 3]
            if prog != 0:
                pmt_pids[prog] = pid
    # PMT per program: elementary PIDs and stream types
    svc = {}
    for prog, ppid in pmt_pids.items():
        for sec in _sections(ts, ppid):
            if not sec or sec[0] != 0x02:
                continue
            slen = ((sec[1] & 0x0F) << 8) | sec[2]
            pil = ((sec[10] & 0x0F) << 8) | sec[11]
            k = 12 + pil
            end = 3 + slen - 4
            vids, auds = [], []
            while k < end - 4:
                stype = sec[k]
                epid = ((sec[k + 1] & 0x1F) << 8) | sec[k + 2]
                esil = ((sec[k + 3] & 0x0F) << 8) | sec[k + 4]
                if stype in _VIDEO_TYPES:
                    vids.append(epid)
                elif stype in _AUDIO_TYPES:
                    auds.append(epid)
                k += 5 + esil
            svc[prog] = {"program": prog, "pmt_pid": ppid,
                         "video_pids": vids, "audio_pids": auds,
                         "video_pid": vids[0] if vids else None}
    # SDT (PID 0x11): service names + CA flag
    for sec in _sections(ts, 0x11):
        if not sec or sec[0] != 0x42:
            continue
        slen = ((sec[1] & 0x0F) << 8) | sec[2]
        k = 11
        end = 3 + slen - 4
        while k < end - 4:
            sid = (sec[k] << 8) | sec[k + 1]
            free_ca = (sec[k + 3] >> 4) & 0x1
            dloop = ((sec[k + 3] & 0x0F) << 8) | sec[k + 4]
            d = sec[k + 5:k + 5 + dloop]
            name = prov = ""
            m = 0
            while m < len(d) - 1:
                tag, dl = d[m], d[m + 1]
                if tag == 0x48 and m + 2 + dl <= len(d):     # service_descriptor
                    p = m + 3
                    pl = d[p]
                    prov = d[p + 1:p + 1 + pl].decode("latin-1", "replace")
                    p += 1 + pl
                    nl = d[p]
                    name = d[p + 1:p + 1 + nl].decode("latin-1", "replace")
                m += 2 + dl
            s = svc.get(sid, {"program": sid})
            s.update(name=name, provider=prov, encrypted=bool(free_ca))
            svc[sid] = s
            k += 5 + dloop
    # NIT (PID 0x10): network name + satellite orbital position
    for sec in _sections(ts, 0x10):
        if not sec or sec[0] not in (0x40, 0x41):
            continue
        ndl = ((sec[8] & 0x0F) << 8) | sec[9]
        d = sec[10:10 + ndl]
        m = 0
        while m < len(d) - 1:
            tag, dl = d[m], d[m + 1]
            if tag == 0x40:                                  # network_name
                result["network_name"] = d[m + 2:m + 2 + dl].decode("latin-1", "replace")
            m += 2 + dl
        # satellite_delivery_system_descriptor (0x43) carries orbital_position (BCD)
        idx = sec.find(b"\x43")
        if idx != -1 and idx + 7 < len(sec):
            bcd = sec[idx + 6:idx + 8]
            try:
                pos = int(f"{bcd[0]:02x}{bcd[1]:02x}") / 10.0
                east = (sec[idx + 8] >> 7) & 1 if idx + 8 < len(sec) else 1
                result["orbital_pos"] = pos if east else -pos
            except Exception:
                pass
    result["services"] = list(svc.values())
    return result
