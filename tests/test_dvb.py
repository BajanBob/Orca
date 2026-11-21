"""Free-to-air DVB-S2 decode orchestration and the conditional-access boundary."""
import asyncio

from orca import dvb


def test_build_pipeline_hackrf_leandvb():
    p = dvb.build_pipeline(11.785e9, 27.5, sdr="hackrf", samp_rate=2.4e6, demod="leandvb")
    assert p["source"][0] == "hackrf_transfer"
    assert "11785000000" in p["source"]              # tuned frequency
    assert p["demod"][0] == "leandvb"
    assert "--sr" in p["demod"] and "27500000" in p["demod"]   # MSym/s -> Sym/s
    assert "--s8" in p["demod"]                       # hackrf signed-8 IQ
    assert p["mux"][0] == "ffmpeg" and "webm" in p["mux"]


def test_build_pipeline_rtlsdr_uses_u8():
    p = dvb.build_pipeline(1.5e9, 2.0, sdr="rtlsdr")
    assert p["source"][0] == "rtl_sdr"
    assert "--u8" in p["demod"]


def test_symrate_accepts_msps_or_sps():
    a = dvb.build_pipeline(1e9, 22.0)["demod"]
    b = dvb.build_pipeline(1e9, 22_000_000)["demod"]
    assert "22000000" in a and "22000000" in b


def test_gate_refuses_conditional_access():
    d = dvb.FtaDecoder()
    r = d.gate(ca_system_id=0x0E00)                   # PowerVu
    assert r["ok"] is False and r["reason"] == "conditional-access"
    assert "does not" in r["message"] and "descramble" in r["message"]


def test_gate_free_to_air_depends_only_on_tools():
    d = dvb.FtaDecoder()
    r = d.gate(ca_system_id=None)                     # FTA: allowed if tools present
    # in CI there are no DVB tools, so it reports tools-missing, never a CA block
    assert r["reason"] in ("ready", "tools-missing")
    if r["reason"] == "tools-missing":
        assert "identified" in r["message"]           # honest: still identifies the carrier


def test_start_refuses_ca_without_spawning():
    d = dvb.FtaDecoder()
    r = asyncio.run(d.start(11.785e9, 27.5, ca_system_id=0x2600))   # BISS
    assert r["ok"] is False and r["reason"] == "conditional-access"
    assert d.running is False and d._procs == []


def test_tools_status_shape():
    t = dvb.tools_status()
    assert set(t) >= {"demod", "demod_ok", "ffmpeg", "sdr_capture", "ready"}
    assert isinstance(t["sdr_capture"], dict)
