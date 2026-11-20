import asyncio
import json
import time

import numpy as np
import pytest

from orca import bands
from orca.adapters.skylark_gnss import GnssMonitor, GnssSample, NmeaState
from orca.adapters.tradewind_aprs import (AprsAdapter, aprs_parse, aprs_position_info, ax25_decode,
                                          ax25_encode, kiss_escape, kiss_frames)
from orca.adapters.uranus_wsjtx import build_decode, build_status, parse
from orca.baseline import Baseline
from orca.capture import CaptureStore
from orca.detect import Rules
from orca.oef import Event, Signer
from orca.sdr import SimSource
from orca.spectrum import Spectrum, detect, psd


def test_event_signing_roundtrip(tmp_path):
    s = Signer(tmp_path / "k.pem")
    ev = s.sign(Event("t", "rf.test", data={"x": np.float32(1.5)}))
    assert Signer.verify(ev, s.public_hex)
    ev.data["x"] = 2
    assert not Signer.verify(ev, s.public_hex)
    assert Signer(tmp_path / "k.pem").public_hex == s.public_hex   # key persisted


def test_plan_dc_to_ku_with_downconverters():
    p = bands.plan(0, 18e9, ["soundcard", "rx888", "hackrf"], ["x_bdc", "ku_lnb_low", "ku_lnb_high", "ku_bdc"])
    assert p.segments[0].receiver == "soundcard"
    used = {(s.receiver, s.frontend) for s in p.segments}
    assert ("rx888", "none") in used and ("hackrf", "x_bdc") in used and ("hackrf", "ku_bdc") in used
    # HackRF alone reaches 6 GHz; 6-8 GHz has no chain in this inventory
    assert any(a >= 6e9 - 1 and b <= 8e9 + 1 for a, b in p.gaps)
    ku = next(s for s in p.segments if s.frontend == "ku_bdc")
    assert ku.tune_hz == pytest.approx(ku.rf_center - 11e9)


def test_inverted_lnb_maps_if():
    fe = bands.FRONTENDS["c_lnb"]
    assert fe.to_if(3.8e9) == pytest.approx(1.35e9)


def test_occupied_bandwidth_measurement():
    src = SimSource(emitters=[], seed=1)
    from orca.sdr import Emitter
    src.emitters = [Emitter("t", 100.0e6 + 200e3, 100e3, 30)]
    iq = src.iq(100e6, 2e6, 2048 * 16)
    f, db = psd(iq, 2e6, 2048)
    ems = detect(f + 100e6, db)
    assert len(ems) == 1
    e = ems[0]
    assert abs(e.f_center - 100.2e6) < 5e3
    assert 80e3 < e.bw_occ < 120e3
    assert e.snr_db > 20


def test_spectrum_sweep_wide_span():
    src = SimSource(seed=2)
    sp = Spectrum(src, ["sim"], [], nbins=512)
    fr = asyncio.run(sp.frame(10e9, 13e9))
    assert fr.mode == "sweep" and len(fr.db) == 512
    assert any(abs(e.f_center - 11325e6) < 150e6 for e in fr.emissions)   # Starlink band visible


def test_baseline_flags_new_signal(tmp_path):
    b = Baseline(tmp_path / "b.sqlite", min_samples=20)
    rng = np.random.default_rng(0)
    for _ in range(60):
        b.update("v", -100 + rng.normal(0, 1, 1024))
    quiet = b.score("v", -100 + rng.normal(0, 1, 1024))
    loud = -100 + rng.normal(0, 1, 1024)
    loud[500:510] = -60
    z = b.score("v", loud)
    assert quiet.max() < 5 and z.max() > 10
    b.save()
    assert Baseline(tmp_path / "b.sqlite").samples("v") == 60


def test_gnss_jamming_and_spoofing():
    m = GnssMonitor("N1")
    for i in range(15):
        m.feed(GnssSample(time.time() + i, [44, 45, 43, 46, 42, 40, 41], 13.1, -59.6, 9))
    evs = m.feed(GnssSample(time.time() + 20, [25, 24, 26, 23], 13.1, -59.6, 2))
    assert any(e.cls == "rf.gnss.jamming" for e in evs)
    m2 = GnssMonitor("N2")
    t = time.time()
    m2.feed(GnssSample(t, [44, 45, 43, 46], 13.10, -59.60, 9))
    evs = m2.feed(GnssSample(t + 1, [44, 45, 43, 46], 13.12, -59.60, 9))   # 2.2 km in 1 s
    assert any(e.cls == "rf.gnss.spoofing" for e in evs)


def test_nmea_parser():
    def cs(body):
        x = 0
        for c in body:
            x ^= ord(c)
        return f"${body}*{x:02X}"
    st = NmeaState()
    st.feed(cs("GPGSV,1,1,04,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45"))
    s = st.feed(cs("GPGGA,123519,1306.18,N,05937.80,W,1,08,0.9,10.0,M,46.9,M,,"))
    assert s and s.used == 8 and sorted(s.cn0) == [39, 41, 45, 46]
    assert s.lat == pytest.approx(13.103, abs=1e-3) and s.lon == pytest.approx(-59.63, abs=1e-3)


def test_ax25_kiss_aprs_roundtrip():
    info = aprs_position_info(13.103, -59.63, "test")
    fr = ax25_encode("8P6AB-9", "APZORC", info, ["WIDE1-1"])
    buf = bytearray([0xC0, 0x00]) + kiss_escape(fr) + bytearray([0xC0])
    out = list(kiss_frames(buf))
    d = ax25_decode(out[0])
    assert d["src"] == "8P6AB-9" and d["path"] == ["WIDE1-1"]
    p = aprs_parse(d["src"], d["info"])
    assert p["kind"] == "position" and p["lat"] == pytest.approx(13.103, abs=1e-3)
    assert aprs_parse("X", ":BLN1     :hello")["text"] == "hello"


def test_aprs_transmit_is_gated():
    a = AprsAdapter("N", mode="sim", callsign="", transmit=True)
    assert asyncio.run(a.send_bulletin("hi")).startswith("not sent")


def test_wsjtx_protocol():
    m = parse(build_decode("CQ K1ABC FN42", -12, 1500))
    assert m["type"] == 2 and m["snr"] == -12 and m["message"] == "CQ K1ABC FN42"
    assert parse(build_status(14074000))["dial_hz"] == 14074000
    assert parse(b"garbage") is None


def test_rules_jamming_cluster_and_correlation():
    r = Rules([], {"A": "Port", "B": "Harbour"})
    r.on_event(Event("s", "rf.gnss.jamming", "high", "A", {"drop_db": 15}))
    (inc, created), = r.on_event(Event("s", "rf.gnss.jamming", "high", "B", {"drop_db": 18}))
    assert not created and "cluster (2 nodes)" in inc["title"]
    gated = [s for s in inc["steps"] if s["gate"]]
    assert gated and all(s["action"] in ("aprs_bulletin", "report") for s in gated)
    r.on_event(Event("net", "net.auth.fail", "medium", "A", {}))
    assert inc["severity"] == "critical" and "network attack" in inc["title"]


def test_sigmf_capture(tmp_path):
    cs = CaptureStore(tmp_path, "N1")
    base = cs.save_iq(np.ones(100, np.complex64), 156.8e6, 250e3, time.time(),
                      [{"f_lo": 1, "f_hi": 2, "bw_occ": 1e3, "snr_db": 20}], "t", "sim")
    meta = json.loads((tmp_path / f"{base}.sigmf-meta").read_text())
    assert meta["global"]["core:datatype"] == "cf32_le"
    assert (tmp_path / f"{base}.sigmf-data").stat().st_size == 800
    with pytest.raises(ValueError):
        cs.path("../etc/passwd")


def test_config_reads_engineering_floats(tmp_path):
    from orca import config
    p = tmp_path / "s.yaml"
    p.write_text("view: {lo: 1565.0e6, hi: 1586e6}\nprotected_extra: [{name: x, lo: 146.994e6, hi: 147.006e6}]\n")
    c = config.load(p)
    assert c["view"]["lo"] == 1565e6 and isinstance(c["protected"][-1]["lo"], float)


def test_gnss_uniform_power_spoof_and_cold_start_jam():
    m = GnssMonitor("N3")
    evs = m.feed(GnssSample(time.time(), [46.1, 45.9, 46.0, 46.2, 45.8, 46.0, 46.1, 45.9], 13.1, -59.6, 8))
    assert any(e.cls == "rf.gnss.spoofing" for e in evs)
    m2 = GnssMonitor("N4")           # jammed from the first sample: no baseline yet
    evs = m2.feed(GnssSample(time.time(), [20, 22, 19, 18, 21], 13.1, -59.6, 1))
    assert any(e.cls == "rf.gnss.jamming" for e in evs)
    m3 = GnssMonitor("N5")           # normal sky: no false spoof alarms
    import random
    random.seed(4)
    bad = 0
    for i in range(300):
        cn = [random.gauss(44, 3.5) for _ in range(random.randint(9, 12))]
        bad += any(e.cls.startswith("rf.gnss.spoof") or e.cls == "rf.gnss.jamming"
                   for e in m3.feed(GnssSample(time.time() + i, cn, 13.1, -59.6, 9)))
    assert bad == 0
