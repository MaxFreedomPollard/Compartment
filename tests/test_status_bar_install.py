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


# --- opting out of the GUI at install time ---------------------------------

def test_a_scripted_install_never_waits_for_a_keypress(monkeypatch, capsys):
    """The window must cost a non-interactive install nothing. A piped stdin
    (installer script, CI, `yes | ...`) has no keyboard behind it, so waiting
    five seconds for input that cannot arrive would be five seconds wasted on
    every automated install."""
    import time as _time
    from compartment import cli

    class NotATerminal:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", NotATerminal())
    start = _time.monotonic()
    assert cli._cli_only_requested(5.0) is False
    assert _time.monotonic() - start < 0.5, "a scripted install must not stall"
    assert "5 seconds" not in capsys.readouterr().out, "and must not be asked"


def test_the_offer_survives_a_terminal_that_refuses_raw_mode(monkeypatch):
    """Some terminals refuse cbreak. The install continues normally rather
    than dying at the very last step, after the vault already exists."""
    from compartment import cli

    class Terminal:
        def isatty(self):
            return True

        def fileno(self):
            raise OSError("no fileno here")

    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    assert cli._cli_only_requested(0.2) is False


def test_the_prompt_says_what_max_asked_it_to_say(monkeypatch, capsys):
    from compartment import cli

    class Terminal:
        def isatty(self):
            return True

        def fileno(self):
            raise OSError("stop here, we only want the text")

    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    cli._cli_only_requested(0.1)
    out = capsys.readouterr().out
    assert 'This is a normal install. Press the letter "s" within 5 seconds' in out
    assert "command-line only install" in out
