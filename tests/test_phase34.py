import json
import math
import time

import numpy as np
import pytest

from orca.df import C, aoa_fix, ll_to_en, tdoa_fix
from orca.intel.classify import Classifier
from orca.intel.fingerprint import EmitterLibrary
from orca.intel.synth import CLASSES, make
from orca.mesh import Loopback, LoopHub, MeshNode, anonymise
from orca.oef import Event, Signer
from orca.secure import (ClearFskDecoder, DecoderBank, KeyStore, SecureChannel,
                         SecurePolicy)


# ---------------------------------------------------------------- Phase 3
def test_modulation_classifier_accuracy(tmp_path):
    clf = Classifier.load_or_train(tmp_path / "m.json", rate=2e6, n=4096, per_class=160, seed=1)
    rng = np.random.default_rng(7)
    ok = n = 0
    for c in CLASSES:
        for _ in range(30):
            n += 1
            ok += clf.classify(make(c, 4096, 2e6, rng), 2e6)["top"] == c
    assert ok / n > 0.82          # ~0.9 in practice; FM/FSK overlap is the floor
    # the cached model reloads and classifies identically
    clf2 = Classifier.load_or_train(tmp_path / "m.json")
    assert clf2.classify(make("am", 4096, 2e6, rng), 2e6)["top"] == "am"


def test_emitter_fingerprint_rematch(tmp_path):
    lib = EmitterLibrary(tmp_path / "e.json")
    rng = np.random.default_rng(3)
    a = make("fsk", 4096, 2e6, rng)
    r1 = lib.observe(a, 2e6, 150e6, 12e3, "fsk", "N1")
    assert not r1["match"]
    r2 = lib.observe(make("fsk", 4096, 2e6, rng), 2e6, 150e6, 12e3, "fsk", "N2")
    assert r2["match"] and r2["id"] == r1["id"] and r2["count"] == 2
    # a different modulation at the same frequency is a new emitter
    r3 = lib.observe(make("am", 4096, 2e6, rng), 2e6, 150e6, 12e3, "am", "N1")
    assert not r3["match"]
    assert EmitterLibrary(tmp_path / "e.json").list()[0]["count"] >= 1


# ---------------------------------------------------------------- secure module
def _policy():
    return SecurePolicy(region=2, floor_hz=148e6, licensed_bands=[(150e6, 174e6), (406e6, 420e6)],
                        authority="LIC-GOV-77")


def test_secure_policy_excludes_amateur_and_below_floor():
    p = _policy()
    assert p.can_secure(155e6)[0]                      # licensed, non-amateur
    assert not p.can_secure(144.39e6)[0]               # 2m amateur / below floor
    assert "amateur" in p.can_secure(435e6)[1]         # 70cm amateur (region 2)
    assert not p.can_secure(145e6)[0]
    assert not p.can_secure(900e6)[0]                  # not licensed
    assert not SecurePolicy(authority="").can_secure(155e6, )[0]   # no authority


def test_own_comms_crypto_roundtrip_and_foreign_key():
    ks = KeyStore()
    kid, key = ks.generate()
    ch = SecureChannel(ks)
    frame = ch.seal(kid, b"secure net message")
    opened = ch.open(frame)
    assert opened["ok"] and opened["plaintext"] == b"secure net message"
    # replay of the same frame is rejected
    assert not ch.open(frame)["ok"]
    # a frame for a key we do not hold is reported, never attacked
    other = KeyStore()
    okid, _ = other.generate()
    foreign = SecureChannel(other).seal(okid, b"not ours")
    res = ch.open(foreign)
    assert res["ok"] is False and res["encrypted"] and res["key_id"] == okid


def test_secure_decoder_demodulates_and_decrypts_own_frame():
    ks = KeyStore()
    kid, key = ks.generate()
    ch = SecureChannel(ks)
    frame = ch.seal(kid, b"FIELD REPORT ALPHA")
    pre = np.tile([1, 0], 64).astype(np.uint8)
    post = np.zeros(32, np.uint8)                 # flush bits, as a real frame has
    bits = np.concatenate([pre, np.unpackbits(np.frombuffer(frame, np.uint8)), post])
    sps, rate = 20, 1e6
    # clean operating point: GCM needs a bit-exact frame, so a real link adds FEC
    iq = np.exp(1j * np.cumsum(np.repeat(bits.astype(float) * 2 - 1, sps) * 0.8)).astype(np.complex64)
    bank = DecoderBank(_policy(), ch)
    bank.register(ClearFskDecoder(baud=rate / sps))
    out = bank.decode(155e6, iq, rate)
    assert out["allowed"]
    r = out["results"][0]
    assert r["decrypted"] and r["text"] == "FIELD REPORT ALPHA"
    # the same decode is refused on an amateur frequency
    assert bank.decode(144.39e6, iq, rate)["allowed"] is False


def test_secure_decoder_reports_but_does_not_break_foreign_encryption():
    ks = KeyStore()            # operator holds no matching key
    ch = SecureChannel(ks)
    other = KeyStore()
    okid, _ = other.generate()
    frame = SecureChannel(other).seal(okid, b"third party traffic")
    pre = np.tile([1, 0], 64).astype(np.uint8)
    post = np.zeros(32, np.uint8)
    bits = np.concatenate([pre, np.unpackbits(np.frombuffer(frame, np.uint8)), post])
    sps, rate = 20, 1e6
    iq = np.exp(1j * np.cumsum(np.repeat(bits.astype(float) * 2 - 1, sps) * 0.8)).astype(np.complex64)
    bank = DecoderBank(_policy(), ch)
    bank.register(ClearFskDecoder(baud=rate / sps))
    r = bank.decode(155e6, iq, rate)["results"][0]
    assert r["encrypted"] and not r["decrypted"] and "does not hold" in r["reason"]


# ---------------------------------------------------------------- Phase 4 DF
def test_aoa_and_tdoa_locate_emitter():
    lat0, lon0 = 13.10, -59.63
    elat, elon = 13.11, -59.60
    nodes = [(13.10, -59.63), (13.07, -59.60), (13.12, -59.64)]
    ex, ey = ll_to_en(elat, elon, lat0, lon0)

    def brg(la, lo):
        x, y = ll_to_en(la, lo, lat0, lon0)
        return math.degrees(math.atan2(ex - x, ey - y)) % 360

    f = aoa_fix([{"lat": la, "lon": lo, "bearing_deg": brg(la, lo)} for la, lo in nodes])
    assert math.dist(ll_to_en(f.lat, f.lon, lat0, lon0), (ex, ey)) < 5

    tn = [{"lat": la, "lon": lo, "t": math.dist(ll_to_en(la, lo, lat0, lon0), (ex, ey)) / C}
          for la, lo in nodes]
    f2 = tdoa_fix(tn)
    assert math.dist(ll_to_en(f2.lat, f2.lon, lat0, lon0), (ex, ey)) < 5
    assert aoa_fix(tn[:1]) is None and tdoa_fix(tn[:2]) is None


def test_tdoa_degrades_gracefully_with_timing_noise():
    lat0, lon0 = 0.0, 0.0
    ex, ey = 3000.0, 2000.0
    nodes = [(0.0, 0.0), (0.0, 0.08), (0.05, 0.0), (0.05, 0.08)]
    rng = np.random.default_rng(0)
    tn = []
    for la, lo in nodes:
        x, y = ll_to_en(la, lo, lat0, lon0)
        tn.append({"lat": la, "lon": lo, "t": math.dist((x, y), (ex, ey)) / C + rng.normal(0, 30e-9)})
    f = tdoa_fix(tn)
    assert math.dist(ll_to_en(f.lat, f.lon, lat0, lon0), (ex, ey)) < 60   # ~9 m typical


# ---------------------------------------------------------------- Phase 4 mesh
def _mesh_line():
    hub = LoopHub()
    sg = {n: Signer() for n in "ABC"}
    pubs = {n: s.public_hex for n, s in sg.items()}
    nodes = {n: MeshNode(n, sg[n], Loopback(hub, n), pubs) for n in "ABC"}
    hub.link("A", "B"); hub.link("B", "C")
    return hub, sg, nodes


def test_mesh_multihop_dedup_and_signature_check():
    hub, sg, nodes = _mesh_line()
    got = []
    nodes["C"].on_event = lambda e: got.append(e["id"])
    ev = sg["A"].sign(Event("skylark.gnss", "rf.gnss.jamming", "high", "A", {"drop_db": 15}))
    nodes["A"].publish(ev.to_dict())
    assert ev.id in got                          # reached C through B
    nodes["A"].publish(ev.to_dict())
    assert got.count(ev.id) == 1                 # duplicate suppressed
    bad = ev.to_dict(); bad["data"] = {"drop_db": 99}; bad["id"] = "evt_bad"
    nodes["B"]._recv(json.dumps({"t": "ev", "hops": 0, "ev": bad}).encode())
    assert nodes["B"].stats()["dropped"] == 1    # tampered event rejected


def test_federation_anonymises():
    ev = Signer().sign(Event("skylark.gnss", "rf.gnss.jamming", "high", "BB-BGI-PORT",
                             {"drop_db": 15}, {"lat": 13.1031, "lon": -59.6287, "err_m": 50})).to_dict()
    a = anonymise(ev, round_m=1000)
    assert "data" not in a and a["site"] == "BB-BGI"
    assert abs(a["geo"]["lat"] - 13.1) < 0.01 and a["geo"]["err_m"] >= 1000


# ---------------------------------------------------------------- troposcatter
from orca import tropo
from orca.mesh import RadioTransport, kiss_wrap


def test_tropo_geometry_matches_oz1rh():
    assert abs(tropo.horizon_km(100) - 41) < 1          # 4.1*sqrt(100)
    assert abs(tropo.takeoff_deg(10, 144e6) - 3.0) < 0.3
    assert abs(tropo.takeoff_deg(40, 144e6) - 0.75) < 0.2


def test_tropo_link_budget_reproduces_amateur_anchors():
    # OZ1RH: 10 W + 20 dB combined ~ 290 km on 2 m SSB is workable
    b = tropo.link_budget(144e6, 290, 10, 10, 10, 20, 20, "ssb")
    assert b.margin_db >= 0 and "feasible" not in b.verdict.split()[0]
    # 100 W + 30 dB ~ 500 km is at least workable
    assert tropo.link_budget(144e6, 500, 100, 15, 15, 30, 30, "ssb").margin_db >= 3
    # 10 W over 700 km with a small station is not feasible
    assert tropo.link_budget(144e6, 700, 10, 10, 10, 20, 20, "ssb").margin_db < 0
    # digital modes (FT8) reach much further than SSB for the same path
    ssb = tropo.link_budget(144e6, 400, 100, 15, 15, 30, 30, "ssb").margin_db
    ft8 = tropo.link_budget(144e6, 400, 100, 15, 15, 30, 30, "ft8").margin_db
    assert ft8 > ssb + 15


def test_tropo_experiment_stats_and_quality():
    b = tropo.link_budget(144e6, 150, 100, 13, 13, 40, 40, "ft8")
    exp = tropo.TropoExperiment("t", b.freq_hz, b.d_km, "ft8", b)
    import numpy as np
    rng = np.random.default_rng(0)
    for i in range(200):
        exp.add(i, tropo.sim_fading_snr(b.snr_db, i, rng))
    st = exp.stats()
    assert st.samples == 200 and st.availability_pct > 90 and st.fade_depth_db > 0
    assert tropo.link_quality(exp.threshold, exp.threshold) == 0.0
    assert tropo.link_quality(exp.threshold + 20, exp.threshold) == 1.0


def test_radio_transport_kiss_roundtrip_and_quality_gate():
    rt = RadioTransport(min_quality=0.15)
    got = []
    rt.set_handler(got.append)
    payload = b'{"t":"ev","ev":{"id":"x"}}'
    rt.feed(kiss_wrap(payload))                       # TNC echoes a framed packet
    assert got and got[0] == payload
    # with no TNC connected or a dead path, sends queue (store-and-forward)
    rt.link_quality = 0.0
    rt.send(payload)
    assert rt.pending == 1 and rt.sent == 0

    class W:
        def __init__(self): self.n = 0
        def write(self, d): self.n += 1
    rt._writer = W(); rt.link_quality = 1.0
    rt.flush()
    assert rt.pending == 0 and rt.sent == 1


# ---------------------------------------------------------------- burst chat
from orca.chat import BurstChat


def _secure_bits():
    ks = KeyStore()
    kid, _ = ks.generate()
    pol = SecurePolicy(region=2, floor_hz=148e6, licensed_bands=[(150e6, 174e6)], authority="LIC-1")
    return ks, kid, pol


def test_burst_chat_clear_delivery():
    wire = []
    tx = BurstChat("N1", "8P6ORC", Signer(), wire.append)
    assert tx.send("net check-in from Barbados", 144.2e6, secure=False)["ok"]
    rx = BurstChat("N2", "8P6XYZ", Signer(), lambda e: None)
    assert rx.on_event(wire[-1]) is True
    assert rx.recent(1)[0]["text"] == "net check-in from Barbados"
    assert rx.recent(1)[0]["secure"] is False


def test_burst_chat_secure_roundtrip_and_boundary():
    ks, kid, pol = _secure_bits()
    wire = []
    tx = BurstChat("N1", "GOV1", Signer(), wire.append, SecureChannel(ks), pol)
    # refused on an amateur frequency and below the floor
    assert tx.send("x", 144.39e6, secure=True)["ok"] is False
    assert tx.send("x", 120e6, secure=True)["ok"] is False
    # allowed on a licensed, non-amateur band; plaintext is not on the wire
    r = tx.send("MOVE TO POINT BRAVO", 155e6, secure=True)
    assert r["ok"] and "MOVE TO POINT" not in str(wire[-1]["data"])
    # a peer holding the key decrypts it
    rx = BurstChat("N2", "GOV2", Signer(), lambda e: None, SecureChannel(ks), pol)
    rx.on_event(wire[-1])
    assert rx.recent(1)[0]["text"] == "MOVE TO POINT BRAVO" and rx.recent(1)[0]["status"] == "decrypted"
    # a peer without the key is told, never attacked
    nokey = BurstChat("N3", "X", Signer(), lambda e: None, SecureChannel(KeyStore()), pol)
    nokey.on_event(wire[-1])
    assert nokey.recent(1)[0]["text"] == "" and "does not hold" in nokey.recent(1)[0]["status"]


def test_burst_chat_secure_needs_module():
    tx = BurstChat("N1", "H", Signer(), lambda e: None)   # no secure channel/policy
    assert tx.send("secret", 155e6, secure=True)["ok"] is False


def test_radio_transport_queues_bursts_through_fades():
    rt = RadioTransport(min_quality=0.15)

    class W:
        def __init__(self): self.n = 0
        def write(self, d): self.n += 1
    rt._writer = W()
    rt.link_quality = 0.0                 # path faded
    for i in range(3):
        rt.send(b"m%d" % i)
    assert rt.pending == 3 and rt.sent == 0
    rt.link_quality = 1.0                 # opening
    rt.flush()
    assert rt.pending == 0 and rt.sent == 3


# ---------------------------------------------------------------- cognition
from orca.cognition import SituationEngine, AskOrca, family
from orca import forecast


def test_situation_engine_correlates_cross_domain():
    eng = SituationEngine({"A": "Port", "B": "Harbour"})
    now = time.time()
    for n in ("A", "B"):
        eng.observe_incident({"id": "INC-" + n, "rule": "gnss_jam", "status": "open",
                              "severity": "high", "nodes": ["A", "B"], "title": "GNSS jamming"})
    eng.observe_event({"class": "net.auth.fail", "node": "A", "ts": now, "data": {}})
    eng.observe_event({"class": "net.auth.fail", "node": "A", "ts": now, "data": {}})
    top = eng.picture(now)[0]
    assert top["id"] == "coordinated-ew-cyber" and top["severity"] == "critical"
    assert "coordinated" in top["assessment"].lower() and top["prediction"]


def test_situation_engine_nominal_and_multinode_denial():
    eng = SituationEngine({"A": "Port", "B": "Harbour"})
    assert eng.picture()[0]["id"] == "nominal"
    for n in ("A", "B"):
        eng.observe_incident({"id": n, "rule": "gnss_jam", "status": "open", "severity": "high",
                              "nodes": ["A", "B"], "title": "x"})
    p = eng.picture()[0]
    assert p["id"] == "gnss-denial" and "area" in p["title"]


def test_ask_orca_answers():
    eng = SituationEngine({"A": "Port"})
    now = time.time()
    eng.observe_event({"class": "rf.gnss.jamming", "node": "A", "ts": now, "data": {}})
    state = {"nodes": [], "incidents": [{"status": "open", "title": "GNSS jamming", "severity": "critical"}],
             "emitters": [], "view": {"lo": 144e6, "hi": 146e6},
             "last_frame": {"emissions": [{"f_center": 145e6, "snr_db": 30, "bw_occ": 12e3}]}, "tropo": None}
    ask = AskOrca(eng, lambda: state)
    assert "jamming" in ask.ask("any jamming this hour?")["answer"].lower()
    assert ask.ask("show gps")["action"]["view"][0] == 1565e6
    assert "145" in ask.ask("strongest signal on 2m")["answer"]
    assert "open" in ask.ask("open incidents")["answer"].lower()
    assert ask.ask("what's the situation?")["situations"]


def test_forecasts():
    assert forecast.anomaly_risk([2, 3, 4, 5, 6, 7], 100)["risk"] > 0.2
    assert forecast.anomaly_risk([], 0)["risk"] == 0.0
    na = forecast.next_appearance([30, 31, 29, 30], time.time() - 5)
    assert na["periodic"] and 25 <= na["period_s"] <= 35
    assert forecast.next_appearance([10, 400], time.time())["periodic"] is False
    op = forecast.tropo_opening(time.time())
    assert 0 <= op["opening"] <= 1 and op["verdict"] in ("enhanced", "normal", "poor")
    assert forecast.tropo_opening(time.time(), pressure_hpa=1028, trend_hpa=1)["opening"] >= \
           forecast.tropo_opening(time.time(), pressure_hpa=1000, trend_hpa=-2)["opening"]


# ---------------------------------------------------------------- satellite
from orca import satcom


def test_satcom_band_resolution_and_symbol_rate():
    assert satcom.band_of(11.785e9).key == "ku"
    assert satcom.band_of(3.95e9).key == "c"          # C-band wins over S in the overlap
    assert satcom.band_of(19.5e9).key == "ka"
    assert satcom.band_of(1.4e9).key == "l"
    assert satcom.band_of(500e6) is None              # not a satellite band
    # symbol rate = BW / (1 + rolloff): a 36 MHz carrier at 0.20 -> 30 MSym/s
    assert abs(satcom.symbol_rate_from_bw(36e6, 0.20) - 30e6) < 1e4
    assert abs(satcom.bw_from_symbol_rate(30e6, 0.20) - 36e6) < 1e4


def test_satcom_profile_picks_standard_and_codecs():
    ku = satcom.profile(11.785e9, 33e6)               # typical DTH transponder
    assert ku.band == "Ku band" and ku.likely_standard == "DVB-S2" and ku.plausible
    assert "HEVC (UHD/4K)" in ku.video_codecs
    wide = satcom.profile(11.325e9, 240e6)            # wideband -> S2X
    assert wide.likely_standard == "DVB-S2X"
    x = satcom.profile(7.5e9, 20e6)                   # military band: identify, don't decode
    assert "encrypt" in x.note.lower() or "military" in x.note.lower()
    assert satcom.profile(500e6, 1e6) is None


def test_satcom_recommendations():
    r = satcom.recommendations()
    assert "DVB-S2X" in r["demodulation"]["standards"]
    assert "HEVC/H.265" in r["video_decode"]["support"]
    assert "never descramble" in r["boundary"] or "not descramble" in r["boundary"] or \
           "descramble it" in r["boundary"]
