"""The icon is supposed to be there until somebody quits it.

A status bar app is not a program you launch, it is a program that is simply
there, like the clock and the battery. Every fault covered here ended the same
way for the user: the icon was gone, nothing said why, and the fix was to know
a command. So these are about persistence rather than appearance.

  - macOS started the app once at login and then stopped caring. Anything
    that ended the process took the icon away until the next login.
  - macOS also reported success for an agent it had failed to load, so an
    install could say "start at login: on" and start nothing.
  - Linux answered the same question by looking at the applications menu,
    which says whether Compartment can be found, not whether it runs.
  - Windows wrote a Run entry without ever reading it back.
"""
import plistlib
import sys

import pytest

from compartment import menubar, systray


# ------------------------------------------------------------------- macOS

def _plist(monkeypatch, tmp_path):
    """Write the login agent into a temp home and hand back the parsed dict."""
    monkeypatch.setattr(menubar.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/bin/true"])
    calls = []
    monkeypatch.setattr(menubar, "_launchctl",
                        lambda *a: (calls.append(a), (0, ""))[1])
    out = menubar._set_login_agent(True)
    path = tmp_path / "Library" / "LaunchAgents" / f"{menubar.LAUNCH_AGENT_LABEL}.plist"
    return out, plistlib.loads(path.read_bytes()), calls, path


def test_launchd_brings_the_icon_back_after_a_crash(monkeypatch, tmp_path):
    _out, data, _calls, _p = _plist(monkeypatch, tmp_path)
    # Not False, and not True either. False is the one-shot that lost the
    # icon on any unexpected exit; plain True would fight the Quit button.
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["RunAtLoad"] is True


def test_quitting_on_purpose_is_still_allowed_to_win(monkeypatch, tmp_path):
    """SuccessfulExit false means launchd relaunches a non-zero exit only.
    Quit goes through NSApp.terminate_, which exits zero, so the icon stays
    gone until the user asks for it back."""
    _out, data, _calls, _p = _plist(monkeypatch, tmp_path)
    assert data["KeepAlive"]["SuccessfulExit"] is False


def test_the_agent_is_actually_loaded_not_just_written(monkeypatch, tmp_path):
    _out, _data, calls, path = _plist(monkeypatch, tmp_path)
    verbs = [c[0] for c in calls]
    assert "bootstrap" in verbs, calls
    # Booted out first, so re-running is idempotent rather than a refusal.
    assert verbs.index("bootout") < verbs.index("bootstrap")
    assert any(str(path) in c for c in calls)


def test_a_refused_agent_is_reported_as_a_failure(monkeypatch, tmp_path):
    """The old code ran `launchctl load`, discarded the result and returned
    "on", so an agent that never loaded looked wired."""
    monkeypatch.setattr(menubar.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/bin/true"])
    monkeypatch.setattr(menubar, "_launchctl",
                        lambda *a: (1, "Load failed: 5: Input/output error"))
    out = menubar._set_login_agent(True)
    assert out.startswith("failed"), out
    assert "Input/output error" in out


def test_turning_it_off_deregisters_before_deleting(monkeypatch, tmp_path):
    """With KeepAlive set, deleting the plist while the job is still
    registered would have launchd relaunch it from a file that is gone."""
    monkeypatch.setattr(menubar.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/bin/true"])
    monkeypatch.setattr(menubar, "_launchctl", lambda *a: (0, ""))
    menubar._set_login_agent(True)

    calls = []
    monkeypatch.setattr(menubar, "_launchctl",
                        lambda *a: (calls.append(a), (0, ""))[1])
    assert menubar._set_login_agent(False) == "off"
    assert calls and calls[0][0] == "bootout"
    path = tmp_path / "Library" / "LaunchAgents" / f"{menubar.LAUNCH_AGENT_LABEL}.plist"
    assert not path.exists()


# ------------------------------------------------------------------- Linux

def test_linux_autostart_is_a_different_file_from_the_menu_entry(monkeypatch,
                                                                 tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    auto = systray.autostart_entry_path()
    menu = systray.desktop_entry_path()
    assert auto != menu
    assert auto.parent.name == "autostart"
    assert menu.parent.name == "applications"


def test_linux_login_status_reads_autostart_not_the_menu(monkeypatch, tmp_path):
    """The bug: an applications-menu entry made this report "on" for a
    machine that had never started Compartment at login."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(systray, "_is_linux", lambda: True)

    systray.install_desktop_entry("/v/memory.vault")     # findable...
    assert systray.login_status() == "off"               # ...but not running

    systray.install_autostart_entry("/v/memory.vault")
    assert systray.login_status() == "on"


def test_linux_autostart_entry_is_one_desktops_will_honour(monkeypatch,
                                                           tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    systray.install_autostart_entry("/v/memory.vault")
    text = systray.autostart_entry_path().read_text()
    assert text.startswith("[Desktop Entry]")
    assert "Type=Application" in text
    # Without these a session is entitled to treat the entry as stale.
    assert "X-GNOME-Autostart-enabled=true" in text
    assert "Hidden=false" in text
    assert "/v/memory.vault" in text


def test_linux_turning_it_off_removes_the_autostart_entry(monkeypatch,
                                                          tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(systray, "_is_linux", lambda: True)
    systray.set_login(True)
    assert systray.login_status() == "on"
    systray.set_login(False)
    assert systray.login_status() == "off"
    assert not systray.autostart_entry_path().exists()


def test_linux_set_login_installs_both(monkeypatch, tmp_path):
    """Turning on start-at-login must not cost the applications-menu entry
    that was there before it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(systray, "_is_linux", lambda: True)
    assert systray.set_login(True) == "on"
    assert systray.autostart_entry_path().is_file()
    assert systray.desktop_entry_path().is_file()


# ----------------------------------------------------------------- Windows

@pytest.mark.skipif(sys.platform != "win32", reason="Run key is Windows-only")
def test_windows_reads_the_run_entry_back(monkeypatch):
    """A write the registry accepted and then dropped used to report "on"."""
    import winreg
    real_query = winreg.QueryValueEx

    def vanished(key, name):
        if name == systray.RUN_VALUE:
            raise FileNotFoundError(name)
        return real_query(key, name)

    monkeypatch.setattr(winreg, "QueryValueEx", vanished)
    assert systray.set_login(True).startswith("error")
