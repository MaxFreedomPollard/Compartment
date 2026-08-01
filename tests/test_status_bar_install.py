"""The status bar app is part of a normal install, on macOS and on Windows.

Unlocking, locking and changing the passphrase are things people do through
the icon. An install that lays down only a CLI has not finished, and until
this it did not: the GUI dependencies were optional extras, `init` never
started the app, and on macOS start-at-login was impossible without the .app
bundle.
"""
import os
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


def test_one_command_covers_every_platform():
    from compartment import cli
    assert 'aliases=["tray", "panel"]' in (
        ROOT / "src/compartment/cli.py").read_text(encoding="utf-8"), \
        "menubar/tray/panel must stay one command"
    assert hasattr(cli, "_start_status_bar_app")


# --- macOS start at login, without an app bundle ---------------------------

@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """A home of our own for everything the login item writes.

    USER_APP_BUNDLE is worked out at import, while HOME was still the real
    one, so the small bundle the login item needs would be written into the
    developer's own ~/Applications however faked the home is. And whether
    this machine has a Compartment.app installed decides what goes in the
    plist, which is not something a test should be reading off the machine
    it happens to run on.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(menubar, "USER_APP_BUNDLE",
                        tmp_path / "Applications" / "Compartment.app")
    monkeypatch.setattr(menubar, "installed_app_bundle", lambda: None)
    return tmp_path


@pytest.fixture()
def launchctl(monkeypatch):
    """launchctl, written down rather than run. Returns the calls.

    The autouse guard in conftest.py has already made the real launchd
    unreachable; this is the same protection with the answers a round trip
    needs, and the record of what was asked is what gets asserted.
    """
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(menubar, "_launchctl",
                        lambda *a: (calls.append(a), (0, ""))[1])
    return calls


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_a_pip_install_is_not_mistaken_for_the_app_bundle():
    """The regression that would put PYTHON in the user's login items.

    A framework Python reports bundleIdentifier "org.python.python" with a
    bundle path inside Python.framework, so a bare "is there a bundle?" test
    passes on an ordinary pip install and SMAppService then acts on Python.app.
    """
    assert menubar._app_service() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_start_at_login_round_trips_without_a_bundle(fake_home, launchctl):
    plist = menubar._agent_plist()
    assert menubar.login_status() == "off"
    assert menubar.set_login(True) == "on"
    assert plist.is_file()
    assert menubar.login_status() == "on"
    assert menubar.set_login(False) == "off"
    assert not plist.exists()
    assert menubar.login_status() == "off"
    # The plist by itself starts nothing, and one left registered after "off"
    # is a job launchd may still bring back. So: booted out before it is
    # bootstrapped, confirmed with launchd rather than with the exit status
    # of the verb, and deregistered before the file is deleted. The lone
    # print in the middle is `login_status` asking launchd the same way.
    assert [c[0] for c in launchctl] == ["bootout", "bootstrap", "print",
                                         "print",
                                         "bootout", "unload"], launchctl
    # And every one of them named our own job. Nothing else on the machine.
    for call in launchctl:
        assert menubar.LAUNCH_AGENT_LABEL in " ".join(call), call


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_the_login_agent_names_a_real_launcher(fake_home, launchctl):
    menubar.set_login(True)
    body = menubar._agent_plist().read_text(encoding="utf-8")
    assert menubar.LAUNCH_AGENT_LABEL in body
    assert "<key>RunAtLoad</key><true/>" in body
    # Not a one-shot: a crash has to bring the icon back, an intentional Quit
    # must not. That is KeepAlive as a dict, never a bare true or false.
    assert "<key>KeepAlive</key>" in body
    assert "<key>SuccessfulExit</key><false/>" in body
    assert "menubar" in body
    menubar.set_login(False)
    assert not menubar._agent_plist().exists()


@pytest.mark.real_launchctl
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS login item")
def test_launchd_really_accepts_the_agent_we_write(fake_home, monkeypatch):
    """The one test that talks to the live launchd.

    Left out of every ordinary run, and asked for by name:

        pytest -m real_launchctl

    Even then it registers a label of its own with a program that does
    nothing, so the job being booted in and out is never the developer's real
    login item and no second menu bar app appears at RunAtLoad.

    It is written with a vault, because launchd reading the file back is the
    only check that the plist is well formed, and the vault is the part of it
    a unit test can only ever compare against itself.
    """
    label = f"{menubar.BUNDLE_ID}.pytest.{os.getpid()}"
    monkeypatch.setattr(menubar, "LAUNCH_AGENT_LABEL", label)
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/usr/bin/true"])
    job = f"{menubar._gui_domain()}/{label}"
    try:
        assert menubar.set_login(True, "/tmp/compartment-pytest.vault") == "on"
        assert menubar._agent_plist().is_file()
        # launchd is holding it, not merely the filesystem - and holding the
        # vault with it, which is how the app finds its way back to the right
        # one when it is started from a bundle that takes no arguments.
        code, out = menubar._launchctl("print", job)
        assert code == 0, out
        assert "/tmp/compartment-pytest.vault" in out, out
        assert menubar.set_login(False) == "off"
        assert not menubar._agent_plist().exists()
        assert menubar._launchctl("print", job)[0] != 0
    finally:
        menubar._launchctl("bootout", job)


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
