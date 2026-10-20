"""When the panel cannot start, it says the one command that fixes it.

pip cannot install tkinter, and "install your distribution's Python Tk
package" left the user to find out what that is called on their system,
which is different on nearly every family - and is the wrong advice for a
Python the distribution did not provide. The tray and menu bar named
`pip install 'compartment[tray]'` and `[menubar]`, extras that have been
empty since their packages became base dependencies.
"""
from __future__ import annotations

import sys
import types

import pytest

from compartment import menubar, systray

UBUNTU = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
MINT = 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'
FEDORA = "ID=fedora\nVERSION_ID=42\n"
ROCKY = 'ID="rocky"\nID_LIKE="rhel centos fedora"\n'
ARCH = "ID=arch\n"
MANJARO = "ID=manjaro\nID_LIKE=arch\n"
TUMBLEWEED = 'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n'
ALPINE = "ID=alpine\n"
VOID = 'ID="void"\n'


@pytest.mark.parametrize("release,command", [
    (UBUNTU, "sudo apt install python3-tk"),
    (MINT, "sudo apt install python3-tk"),
    (FEDORA, "sudo dnf install python3-tkinter"),
    (ROCKY, "sudo dnf install python3-tkinter"),
    (ARCH, "sudo pacman -S tk"),
    (MANJARO, "sudo pacman -S tk"),
    (TUMBLEWEED, "sudo zypper install python3-tk"),
    (ALPINE, "sudo apk add python3-tkinter"),
    (VOID, "sudo xbps-install python3-tkinter"),
])
def test_each_family_gets_its_own_package(release, command):
    assert systray.tk_install_hint(release, base_prefix="/usr",
                                   platform="linux") == command


def test_a_python_the_distribution_did_not_provide_is_told_to_rebuild():
    """The distribution's package does nothing for pyenv's Python: its
    _tkinter is built with the Python, against Tk's headers."""
    hint = systray.tk_install_hint(UBUNTU,
                                   base_prefix="/home/u/.pyenv/versions/3.12.4",
                                   platform="linux")
    assert hint.startswith("sudo apt install tk-dev")
    assert "pyenv install --force" in hint
    assert "python3-tk" not in hint


def test_conda_and_the_bsds_and_termux(tmp_path):
    (tmp_path / "conda-meta").mkdir()
    assert systray.tk_install_hint("", base_prefix=str(tmp_path),
                                   platform="linux") == "conda install tk"
    ver = f"{sys.version_info[0]}{sys.version_info[1]}"
    assert systray.tk_install_hint("", base_prefix="/usr/local",
                                   platform="freebsd14") == \
        f"sudo pkg install py{ver}-tkinter"
    assert systray.tk_install_hint("", base_prefix="/usr/local",
                                   platform="openbsd7") == \
        "doas pkg_add python-tkinter"
    assert "python-tkinter" in systray.tk_install_hint(
        "", base_prefix="/data/data/com.termux/files/usr", platform="linux")


def test_an_unknown_distribution_still_gets_a_useful_line():
    hint = systray.tk_install_hint("ID=somethingnew\n", base_prefix="/usr",
                                   platform="linux")
    assert "python3-tk" in hint and "python3-tkinter" in hint


def _stand_alone(monkeypatch, platform: str) -> None:
    """run() up to the point where it imports its toolkit."""
    monkeypatch.setattr(systray.sys, "platform", platform)
    monkeypatch.setattr(systray, "acquire_instance_lock",
                        lambda vault: (None, True))
    monkeypatch.setattr(systray, "hand_over_to_supervisor", lambda vault: False)


def test_a_panel_without_tkinter_prints_this_machines_command(
        monkeypatch, capsys, tmp_path):
    _stand_alone(monkeypatch, "linux")
    monkeypatch.setitem(sys.modules, "tkinter", None)
    monkeypatch.setattr(systray, "_os_release", lambda: ARCH)
    monkeypatch.setattr(systray.sys, "base_prefix", "/usr")
    assert systray.run(str(tmp_path / "v.vault")) == 3
    err = capsys.readouterr().err
    assert "sudo pacman -S tk" in err
    assert sys.executable in err, "say which Python it is about"
    assert "uv tool install compartment" in err and "dash" in err


def _fake_tk(monkeypatch, error: str):
    tk = types.ModuleType("tkinter")

    class TclError(Exception):
        pass

    def Tk():                                           # noqa: N802
        raise TclError(error)

    tk.TclError, tk.Tk = TclError, Tk
    tk.ttk = types.ModuleType("tkinter.ttk")
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", tk.ttk)


def test_a_wayland_session_without_xwayland_is_told_so(monkeypatch, capsys,
                                                       tmp_path):
    """Tk draws through X11 only. "No graphical display" on a desktop that
    is plainly running sends the user looking for the wrong problem."""
    _stand_alone(monkeypatch, "linux")
    _fake_tk(monkeypatch, "no display name and no $DISPLAY environment "
                          "variable")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert systray.run(str(tmp_path / "v.vault")) == 4
    err = capsys.readouterr().err
    assert "Wayland" in err and "XWayland" in err
    assert "Traceback" not in err


def test_a_headless_box_still_gets_the_headless_advice(monkeypatch, capsys,
                                                       tmp_path):
    _stand_alone(monkeypatch, "linux")
    _fake_tk(monkeypatch, "no display name and no $DISPLAY environment "
                          "variable")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert systray.run(str(tmp_path / "v.vault")) == 4
    err = capsys.readouterr().err
    assert "no graphical display" in err and "compartment dash" in err


def test_a_python_with_broken_tcl_files_is_not_called_headless(
        monkeypatch, capsys, tmp_path):
    _stand_alone(monkeypatch, "linux")
    _fake_tk(monkeypatch, "Can't find a usable init.tcl in the following "
                          "directories: /install/lib/tcl8.6")
    monkeypatch.setenv("DISPLAY", ":0")
    assert systray.run(str(tmp_path / "v.vault")) == 4
    err = capsys.readouterr().err
    assert "Tcl/Tk files" in err and "no graphical display" not in err


def test_the_tray_names_packages_that_install_something(monkeypatch, capsys,
                                                        tmp_path):
    _stand_alone(monkeypatch, "win32")
    _fake_tk(monkeypatch, "unused")
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert systray.run(str(tmp_path / "v.vault")) == 3
    err = capsys.readouterr().err
    assert f'"{sys.executable}" -m pip install pystray pillow' in err
    assert "[tray]" not in err, "an empty extra installs nothing"


def test_the_menu_bar_names_a_package_that_installs_something(monkeypatch,
                                                              capsys):
    monkeypatch.setattr(menubar.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "objc", None)
    assert menubar.run("/tmp/v.vault") == 1
    err = capsys.readouterr().err
    assert f'"{sys.executable}" -m pip install pyobjc-framework-Cocoa' in err
    assert "[menubar]" not in err
