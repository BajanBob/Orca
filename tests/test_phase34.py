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
