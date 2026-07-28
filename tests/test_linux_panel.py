"""Linux gets the panel as a window, and is findable without a tray icon.

A tray icon on Linux appears or does not depending on the desktop, and on
GNOME or Wayland it can simply never show up with nothing said. For the
control that unlocks your memories that is the worst possible failure, so
Linux draws the same panel as an ordinary window and puts itself in the
applications menu instead.
"""
import os
import sys

import pytest

from compartment import cli, menubar, systray


@pytest.fixture()
def as_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    return tmp_path


# ------------------------------------------------------------- which front end

def test_linux_uses_the_panel_not_the_menu_bar(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli._tray_app() is systray


def test_macos_is_untouched(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli._tray_app() is menubar


def test_no_tray_icon_is_attempted_on_linux(monkeypatch):
    """pystray is a Windows dependency and is not installed on Linux, so
    reaching for it there would end the app before it drew anything."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert systray.has_tray() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert systray.has_tray() is True


def test_panel_is_a_name_for_the_same_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_menubar", lambda args: seen.update(vars(args)))
    cli.main(["panel", "--self-check"])
    assert seen["self_check"] is True
    seen.clear()
    cli.main(["menubar", "--self-check"])
    assert seen["self_check"] is True


# --------------------------------------------------------- the menu entry

def test_installing_puts_compartment_in_the_applications_menu(as_linux):
    out = systray.install_desktop_entry("/home/x/.compartment/memory.vault")
    assert not out.startswith("error")
    path = systray.desktop_entry_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]")
    assert "Name=Compartment" in text
    assert "Type=Application" in text


def test_the_entry_opens_the_panel_for_the_right_vault(as_linux):
    systray.install_desktop_entry("/home/x/other.vault")
    text = systray.desktop_entry_path().read_text(encoding="utf-8")
    exec_line = [l for l in text.splitlines() if l.startswith("Exec=")][0]
    assert "panel" in exec_line
    assert "/home/x/other.vault" in exec_line, (
        "a user running a second vault must not silently get the default one")


def test_the_entry_does_not_open_a_terminal(as_linux):
    systray.install_desktop_entry()
    assert "Terminal=false" in \
        systray.desktop_entry_path().read_text(encoding="utf-8")


def test_the_entry_has_an_icon_that_linux_can_draw(as_linux):
    """The Windows tray icon is a .ico, which not every desktop renders."""
    systray.install_desktop_entry()
    text = systray.desktop_entry_path().read_text(encoding="utf-8")
    icon = [l for l in text.splitlines() if l.startswith("Icon=")][0]
    assert icon.endswith(".png")
    assert systray.app_icon_path().is_file()


def test_the_icon_ships_with_the_package():
    assert systray.app_icon_path().is_file()
    assert systray.app_icon_path().stat().st_size > 1000


def test_the_entry_honours_xdg_data_home(as_linux, tmp_path):
    assert str(tmp_path / "share") in str(systray.desktop_entry_path())


def test_login_status_reflects_the_entry(as_linux):
    assert systray.login_status() == "off"
    systray.set_login(True)
    assert systray.login_status() == "on"
    systray.set_login(False)
    assert systray.login_status() == "off"
    assert not systray.desktop_entry_path().exists()


def test_removing_it_twice_is_not_an_error(as_linux):
    systray.set_login(True)
    assert systray.set_login(False) == "off"
    assert systray.set_login(False) == "off"


def test_an_unwritable_data_dir_reports_instead_of_raising(as_linux,
                                                           monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(systray.Path, "mkdir", boom)
    assert systray.install_desktop_entry().startswith("error")


# --------------------------------------------------------------- the install

def test_init_tells_a_linux_user_what_it_did(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cli, "_linux_gui_available", lambda: True)
    fake = type("A", (), {"set_login": staticmethod(lambda v: "on")})
    monkeypatch.setattr(cli, "_tray_app", lambda: fake)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: None)
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    out = capsys.readouterr().out
    assert "applications menu" in out
    assert "start at login" not in out, (
        "nothing starts at login on Linux: a window in your face at every "
        "sign-in is not what a management app should do")


def test_a_python_without_tkinter_says_the_one_command_that_fixes_it(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cli, "_linux_gui_available", lambda: False)
    fake = type("A", (), {"set_login": staticmethod(lambda v: "on")})
    monkeypatch.setattr(cli, "_tray_app", lambda: fake)
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: started.append(a))
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    out = capsys.readouterr().out
    assert "uv tool install compartment" in out
    assert "python3-tk" in out
    assert "dash" in out, "say what does work, not only what does not"
    assert started == [], "never launch a panel that cannot draw"


def test_a_headless_box_gets_no_window_and_no_menu_entry(monkeypatch, capsys,
                                                         tmp_path):
    """Most Linux installs are servers. A window there fails and a menu
    entry is litter, and neither is a broken install."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    calls = []
    fake = type("A", (), {"set_login": staticmethod(
        lambda v: calls.append(v) or "on")})
    monkeypatch.setattr(cli, "_tray_app", lambda: fake)
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: started.append(a))
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    out = capsys.readouterr().out
    assert "headless" in out.lower()
    assert "dash" in out
    assert calls == [], "no applications menu on a machine with no desktop"
    assert started == []


def test_a_desktop_session_is_detected(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert cli._linux_has_display() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert cli._linux_has_display() is True
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert cli._linux_has_display() is True


def test_the_gui_check_is_honest_about_this_machine():
    """It reports what this interpreter can actually do, rather than
    guessing from the platform."""
    try:
        import tkinter                                  # noqa: F401
        expected = True
    except Exception:                                   # noqa: BLE001
        expected = False
    assert cli._linux_gui_available() is expected


# ------------------------------------------------------------------ cleanup

def test_stopping_a_running_panel_uses_signals_not_taskkill(monkeypatch):
    """taskkill does not exist off Windows, so update and uninstall would
    have left the old panel running with its binary already gone."""
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []

    class R:
        stdout = ""
    monkeypatch.setattr(systray.subprocess, "run",
                        lambda *a, **k: (calls.append(a[0]), R())[1])
    systray.quit_running()
    assert calls and calls[0][0] == "pgrep"


def test_it_never_signals_itself(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    class R:
        stdout = str(os.getpid())
    monkeypatch.setattr(systray.subprocess, "run", lambda *a, **k: R())
    killed = []
    monkeypatch.setattr(systray.os, "kill",
                        lambda pid, sig: killed.append(pid))
    systray.quit_running()
    assert killed == [], "an uninstall that kills its own process finishes nothing"
