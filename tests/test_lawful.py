"""Lawful-access profile: governance + audit, and the hard CA refusal."""
import time

from orca.lawful import LawfulAccessProfile, enforcement_report, from_config
from orca.secure import SecurePolicy


def _profile(**kw):
    pol = SecurePolicy(region=2, floor_hz=148e6,
                       licensed_bands=[(400e6, 420e6)], authority="Regulator licence R-42")
    return LawfulAccessProfile(policy=pol, warrant_ref="WARRANT-2026-001",
                               services=["GOV-LINK-1"], **kw)


def test_refuses_conditional_access_even_with_warrant():
    p = _profile()
    r = p.authorize(11.785e9, "PayTV", ca_system_id=0x0E00)   # PowerVu
    assert r["allowed"] is False and r["reason"] == "conditional-access"
    assert "never descrambles" in r["message"]
    assert p.audit[-1]["warrant_ref"] == "WARRANT-2026-001"   # refusal still logged


def test_authorises_own_comms_on_licensed_band():
    p = _profile()
    r = p.authorize(410e6, "GOV-LINK-1", ca_system_id=None)
    assert r["allowed"] is True and r["reason"] == "authorised"


def test_refuses_amateur_band():
    p = _profile()
    r = p.authorize(432e6, "GOV-LINK-1")        # 70 cm amateur
    assert r["allowed"] is False and r["reason"] == "policy"


def test_refuses_without_warrant():
    pol = SecurePolicy(region=2, floor_hz=148e6, licensed_bands=[(400e6, 420e6)],
                       authority="R-42")
    p = LawfulAccessProfile(policy=pol, warrant_ref="", services=[])
    r = p.authorize(410e6, "x")
    assert r["allowed"] is False and r["reason"] == "no-warrant"


def test_out_of_scope_service_and_window():
    p = _profile(valid_from=0, valid_to=time.time() - 10)      # already expired
    r = p.authorize(410e6, "GOV-LINK-1")
    assert r["allowed"] is False and r["reason"] == "out-of-window"
    p2 = _profile()
    r2 = p2.authorize(410e6, "SOME-OTHER-SERVICE")
    assert r2["allowed"] is False and r2["reason"] == "out-of-scope"


def test_enforcement_report_flags_without_decrypting():
    rep = enforcement_report([
        {"freq_hz": 410e6, "licensed": True, "ca_system_id": None, "identified": True},
        {"freq_hz": 11.785e9, "licensed": True, "ca_system_id": 0x2600, "identified": True},  # BISS
        {"freq_hz": 155e6, "licensed": False, "ca_system_id": None, "identified": False},
    ])
    assert rep["carriers"] == 3 and rep["flagged"] == 2
    reasons = " ".join(r for item in rep["items"] for r in item["reasons"])
    assert "BISS" in reasons and "unlicensed" in reasons and "unidentified" in reasons
    assert "never descrambled" in rep["note"]


def test_from_config_disabled_returns_none():
    assert from_config({"lawful": {"enabled": False}, "secure": {}}) is None


def test_from_config_builds_profile():
    cfg = {"lawful": {"enabled": True, "warrant_ref": "W-1", "services": ["S1"]},
           "secure": {"floor_hz": 148e6, "licensed_bands": [[400e6, 420e6]], "authority": "A"}}
    p = from_config(cfg, region=2)
    assert p and p.warrant_ref == "W-1" and p.policy.authority == "A"
