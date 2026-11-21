"""USB/serial radio discovery and profile matching (no hardware required)."""
from orca import devices
from orca.devices import match_profile


def test_hackrf_by_usb_id():
    p = match_profile(0x1D50, 0x6089, "HackRF One")
    assert p and p.key == "hackrf"
    assert p.subsystem == "sdr" and p.rx_only is True


def test_ic9100_cp210x_by_description():
    # IC-9100 rides a Silicon Labs CP210x bridge; the description names the rig.
    p = match_profile(0x10C4, 0xEA60, "Silicon Labs CP210x USB to UART Bridge (IC-9100)")
    assert p and p.key == "ic9100"
    assert p.subsystem == "cat" and p.bind == "uranus"
    assert p.rx_only is False


def test_yaesu_ftm510_by_keyword():
    p = match_profile(0x067B, 0x2303, "Yaesu FTM-510DR data cable")
    assert p and p.key == "ftm510"
    assert p.subsystem == "tnc" and p.bind == "aprs"


def test_shared_bridge_tie_breaks_on_description():
    # Same CP210x VID:PID is claimed by both IC-9100 and FTM-510DR profiles;
    # a Yaesu description must resolve to the Yaesu, not the Icom.
    p = match_profile(0x10C4, 0xEA60, "Yaesu FTM-510DR")
    assert p and p.key == "ftm510"


def test_unknown_device_is_none():
    assert match_profile(0x1234, 0x5678, "Some random widget") is None


def test_config_fragment_is_rx_safe_for_aprs():
    p = match_profile(None, None, "Yaesu FTM-510DR")
    frag = devices._yaml_fragment(p, "COM5")
    assert "transmit: false" in frag        # never auto-enable TX
    assert "mode: kiss" in frag


def test_detect_degrades_without_hardware():
    # With neither pyserial devices nor SoapySDR present, detect() is empty, not an error.
    out = devices.detect()
    assert isinstance(out, list)


def test_report_runs():
    assert "USB" in devices.report()
