"""Shared radio catalog + Windows driver / plug-and-play model."""
from orca import drivers, radios


def test_catalog_loads_full_tradewind_list():
    cat = radios.catalog()
    ids = {r["id"] for r in cat}
    assert len(cat) == 33
    # the radios this task is about, plus a spread across vendors
    for want in ("ic9100", "signalink", "ft991a", "yaesu_ftm", "ic7300",
                 "thd75", "ts590", "rtlsdr_direwolf", "rigctld"):
        assert want in ids, want
    assert "Tradewind Commander" in radios.source()


def test_ic9100_maps_to_cat_subsystem_and_serial_driver():
    e = radios.entry(radios.get("ic9100"))
    assert e["bind"] == "uranus"
    ifaces = {d["interface"] for d in e["driver"]["drivers"]}
    assert drivers.USB_AUDIO in ifaces        # built-in USB sound card
    assert drivers.USB_SERIAL in ifaces       # CI-V on the COM port
    assert e["plug_and_play"] is True


def test_signalink_is_usb_audio_class_no_driver():
    e = radios.entry(radios.get("signalink"))
    ifaces = {d["interface"] for d in e["driver"]["drivers"]}
    assert ifaces == {drivers.USB_AUDIO}      # SignaLink = TI USB codec, in-box driver
    assert e["plug_and_play"] is True


def test_sdr_dongle_needs_winusb_but_is_automatable(monkeypatch):
    monkeypatch.setattr(drivers.sys, "platform", "win32")
    plan = drivers.driver_plan(radios.get("rtlsdr_direwolf"))
    sdr = [n for n in plan if n.interface == drivers.WINUSB]
    assert sdr and sdr[0].install == "libwdi" and sdr[0].auto is True


def test_aprsis_only_is_software_no_usb_driver():
    e = radios.entry(radios.get("aprsis_only"))
    ifaces = {d["interface"] for d in e["driver"]["drivers"]}
    assert ifaces == {drivers.NONE}


def test_rigctld_still_needs_a_sound_card():
    # CAT/PTT is via Hamlib software, but audio still rides a USB sound device.
    e = radios.entry(radios.get("rigctld"))
    ifaces = {d["interface"] for d in e["driver"]["drivers"]}
    assert drivers.USB_AUDIO in ifaces and e["plug_and_play"] is True


def test_install_winusb_non_windows_is_noop():
    import orca.drivers as d
    r = d.install_winusb(0x0bda, 0x2838)
    assert r["ok"] is True and "libusb" in r["message"].lower()


def test_install_winusb_windows_without_libwdi_falls_back_to_zadig(monkeypatch):
    monkeypatch.setattr(drivers.sys, "platform", "win32")
    r = drivers.install_winusb(0x1d50, 0x6089, "HackRF One")
    # no libwdi in the test env -> safe, non-raising Zadig fallback
    assert r["ok"] is False and r["method"] == "zadig-fallback"
    assert "Zadig" in r["message"]


def test_all_catalog_radios_are_plug_and_play_on_this_os():
    # every supported radio must resolve to an automatable driver path (no manual
    # install that Orca can't guide) so the catalog stays "plug and play".
    for e in radios.all_entries():
        assert e["plug_and_play"] is True, e["id"]


def test_report_renders():
    assert "radio catalog" in radios.report()
