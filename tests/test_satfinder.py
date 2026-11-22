"""Satellite finder: look-angles, low-SNR detection, and TS identification."""
import numpy as np

from orca import satfinder


# ----------------------------------------------------------------- look angles
def test_look_angle_due_south_same_longitude():
    la = satfinder.look_angle(13.1, -59.6, -59.6)      # Barbados -> sat at same lon
    assert la["visible"] is True
    assert 170 <= la["azimuth_deg"] <= 190              # due south
    assert la["elevation_deg"] > 60                     # high elevation near equator


def test_look_angle_below_horizon():
    la = satfinder.look_angle(13.1, -59.6, 120.0)       # far-east sat from the Caribbean
    assert la["visible"] is False and la["elevation_deg"] < 0


def test_look_angle_southern_hemisphere_points_north():
    la = satfinder.look_angle(-33.9, 18.4, 18.4)        # Cape Town -> sat at same lon
    assert la["visible"] is True
    assert la["azimuth_deg"] < 10 or la["azimuth_deg"] > 350   # due north


def test_identify_satellite():
    hit = satfinder.identify_satellite(19.0)[0]
    assert "19.2E" in hit["name"] and hit["delta_deg"] <= 0.3


# --------------------------------------------------------------- C/N detection
def test_cn_estimate_detects_carrier_above_noise():
    rate, n = 2.4e6, 8192
    rng = np.random.default_rng(0)
    noise = (rng.normal(size=n) + 1j*rng.normal(size=n)).astype(np.complex64) * 0.3
    t = np.arange(n)/rate
    carrier = np.exp(2j*np.pi*50e3*t).astype(np.complex64)
    strong = satfinder.cn_estimate(noise + carrier, rate)
    weak = satfinder.cn_estimate(noise, rate)
    assert strong["cn_db"] > weak["cn_db"]
    assert strong["strength"] > weak["strength"]
    assert 0.0 <= strong["strength"] <= 1.0


def test_cn_estimate_empty_safe():
    r = satfinder.cn_estimate(np.zeros(8, np.complex64), 2.4e6)
    assert r["strength"] == 0.0 and r["locked_hint"] is False


# --------------------------------------------- TS identification (hand-built)
def _section_packet(pid, section):
    sec = bytes(section) + b"\x00\x00\x00\x00"          # fake CRC (parser ignores it)
    pkt = bytearray([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0x00])  # PUSI, pointer 0
    pkt += sec
    pkt += b"\xFF" * (188 - len(pkt))
    return bytes(pkt[:188])


def _len_hdr(tid, payload):
    slen = len(payload) + 4                              # + CRC
    return bytes([tid, 0xB0 | (slen >> 8), slen & 0xFF]) + payload


def test_parse_ts_extracts_pids_name_and_fta_flag():
    # PAT: program 1 -> PMT PID 0x100
    pat_body = b"\x00\x01\xC1\x00\x00" + b"\x00\x01\xE1\x00"
    pat = _len_hdr(0x00, pat_body)
    # PMT: program 1, PCR 0x200, H.264 video PID 0x200, MP2 audio PID 0x201
    pmt_body = (b"\x00\x01\xC1\x00\x00" + b"\xE2\x00" + b"\x00\x00"
                + b"\x1B\xE2\x00\x00\x00" + b"\x03\xE2\x01\x00\x00")
    pmt = _len_hdr(0x02, pmt_body)
    # SDT: service sid 1, free-to-air, name "News", provider "Gov"
    sd = b"\x01" + bytes([len(b"Gov")]) + b"Gov" + bytes([len(b"News")]) + b"News"
    svc_desc = bytes([0x48, len(sd)]) + sd
    sdt_body = (b"\x00\x01\xC1\x00\x00" + b"\x00\x01\xFF"
                + b"\x00\x01\xFF" + bytes([0x80 | (len(svc_desc) >> 8), len(svc_desc) & 0xFF])
                + svc_desc)
    sdt = _len_hdr(0x42, sdt_body)
    ts = _section_packet(0x00, pat) + _section_packet(0x100, pmt) + _section_packet(0x11, sdt)

    out = satfinder.parse_ts(ts)
    svcs = {s.get("program"): s for s in out["services"]}
    assert 1 in svcs
    s = svcs[1]
    assert s["video_pid"] == 0x200
    assert 0x201 in s["audio_pids"]
    assert s.get("name") == "News" and s.get("provider") == "Gov"
    assert s.get("encrypted") is False


def test_parse_ts_marks_encrypted_service():
    sd = b"\x01\x00\x03Pay"
    svc_desc = bytes([0x48, len(sd)]) + sd
    # free_CA_mode = 1 (top bit of the running/free byte)
    sdt_body = (b"\x00\x01\xC1\x00\x00" + b"\x00\x01\xFF"
                + b"\x00\x02\xFF" + bytes([0x90 | (len(svc_desc) >> 8), len(svc_desc) & 0xFF])
                + svc_desc)
    sdt = _len_hdr(0x42, sdt_body)
    out = satfinder.parse_ts(_section_packet(0x11, sdt))
    s = {x.get("program"): x for x in out["services"]}[2]
    assert s["encrypted"] is True              # identified as encrypted; never descrambled
