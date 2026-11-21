"""Regression test: the windowed Windows build has no console (sys.stdout and
sys.stderr are None). Starting the desktop server must not crash on logging setup."""
import sys

import uvicorn

from orca import desktop


def test_windowed_build_without_console(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop.ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None
    assert (tmp_path / "Orca" / "orca-desktop.log").exists()
    # The exact call that crashed: building uvicorn's config with default logging
    uvicorn.Config(lambda scope, receive, send: None, host="127.0.0.1", port=0,
                   log_level="warning", log_config=None)
    uvicorn.Config(lambda scope, receive, send: None, host="127.0.0.1", port=0,
                   log_level="warning")
