"""The status bar app is part of a normal install, on macOS and on Windows.

Unlocking, locking and changing the passphrase are things people do through
the icon. An install that lays down only a CLI has not finished, and until
this it did not: the GUI dependencies were optional extras, `init` never
started the app, and on macOS start-at-login was impossible without the .app
bundle.
"""
import sys
import tomllib
from pathlib import Path

import pytest

from compartment import menubar

ROOT = Path(__file__).resolve().parents[1]


# --- one install, not two --------------------------------------------------

def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_gui_deps_ship_with_a_plain_install():
    """`pip install compartment` has to be enough. No extra, no second step."""
    deps = " ".join(_pyproject()["project"]["dependencies"])
    assert "pyobjc-framework-Cocoa" in deps, "macOS status bar missing"
    assert "pystray" in deps and "pillow" in deps, "Windows tray missing"


def test_the_gui_deps_are_platform_gated():
    """Nothing macOS-only on Windows, nothing Windows-only on macOS."""
    for dep in _pyproject()["project"]["dependencies"]:
        if "pyobjc" in dep:
            assert "sys_platform == 'darwin'" in dep
        if "pystray" in dep or "pillow" in dep:
            assert "sys_platform == 'win32'" in dep


def test_the_old_extras_still_resolve():
    """Someone's script or notes may still say compartment[menubar]. That must
    keep working rather than failing on an unknown extra."""
    extras = _pyproject()["project"]["optional-dependencies"]
    assert extras["menubar"] == [] and extras["tray"] == []


def test_one_command_covers_both_platforms():
    from compartment import cli
    assert 'aliases=["tray"]' in (ROOT / "src/compartment/cli.py").read_text(
        encoding="utf-8"), "menubar/tray must stay one command"
    assert hasattr(cli, "_start_status_bar_app")


# --- macOS start at login, without an app bundle ---------------------------

@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_a_pip_install_is_not_mistaken_for_the_app_bundle():
    """The regression that would put PYTHON in the user's login items.

    A framework Python reports bundleIdentifier "org.python.python" with a
    bundle path inside Python.framework, so a bare "is there a bundle?" test
    passes on an ordinary pip install and SMAppService then acts on Python.app.
    """
    assert menubar._app_service() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_start_at_login_round_trips_without_a_bundle(fake_home):
    plist = menubar._agent_plist()
    assert menubar.login_status() == "off"
    assert menubar.set_login(True) == "on"
    assert plist.is_file()
    assert menubar.login_status() == "on"
    assert menubar.set_login(False) == "off"
    assert not plist.exists()
    assert menubar.login_status() == "off"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_the_login_agent_names_a_real_launcher(fake_home):
    menubar.set_login(True)
    body = menubar._agent_plist().read_text(encoding="utf-8")
    assert menubar.LAUNCH_AGENT_LABEL in body
    assert "<key>RunAtLoad</key><true/>" in body
    assert "menubar" in body
    menubar.set_login(False)


# --- Windows autostart -----------------------------------------------------

def test_windows_autostart_avoids_a_console_window_and_keeps_the_vault():
    """python.exe opens a console window behind an app that has no window, at
    every sign-in, and an autostart that drops --vault silently sends a user
    with a non-default vault back to the default one."""
    from compartment import systray
    cmd = systray._autostart_command("/tmp/some.vault")
    assert "/tmp/some.vault" in cmd, "the vault path must survive a reboot"
    assert "python.exe" not in cmd.lower() or "pythonw.exe" in cmd.lower()
