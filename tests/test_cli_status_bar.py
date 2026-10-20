"""What `compartment init` does to put the status bar app up: nothing
registered for a panel that cannot draw, and no console window behind the
Windows tray app."""
import subprocess
import sys
import types

from compartment import cli


class _Running:
    """A child still running when the installer looks."""

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("tray", timeout)


def test_a_python_without_tkinter_registers_nothing_to_start_at_login(
        monkeypatch, capsys, tmp_path):
    """Start at login was registered before tkinter was checked, so a Linux
    Python without it got an autostart entry or a systemd user unit that
    failed at every login, with nothing on screen to say why."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cli, "_linux_gui_available", lambda: False)
    registered = []
    app = types.SimpleNamespace(
        set_login=lambda on, vault=None: registered.append(on) or "on",
        tk_install_hint=lambda: "sudo zypper install python312-tk")
    monkeypatch.setattr(cli, "_tray_app", lambda: app)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("launched")))
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    out = capsys.readouterr().out
    assert registered == []
    assert "sudo zypper install python312-tk" in out
    assert "uv tool install compartment" in out
    assert "compartment panel --login on" in out
    assert "start at login: on" not in out


def _windows(monkeypatch, tmp_path, pythonw: bool):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    names = ["python.exe", "compartment.exe"] + (["pythonw.exe"] if pythonw
                                                else [])
    for name in names:
        (scripts / name).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")

    def parts(vault=None):          # systray._autostart_parts, in short
        pyw = scripts / "pythonw.exe"
        return ((str(pyw), "-m compartment.cli tray") if pyw.is_file()
                else (str(scripts / "compartment.exe"), "tray"))
    app = types.SimpleNamespace(set_login=lambda on, vault=None: "on",
                                start_supervised=lambda: False,
                                _autostart_parts=parts)
    monkeypatch.setattr(cli, "_tray_app", lambda: app)
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name, *a, **k: str(scripts / f"{name}.exe"))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: True)
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **k:
                        started.append((argv, k)) or _Running())
    return scripts, started


def test_windows_starts_the_tray_with_pythonw(monkeypatch, tmp_path, capsys):
    """The console script compartment.exe was started with DETACHED_PROCESS,
    and the Python it runs could still open a console window of its own
    behind a tray app that has no window. pythonw.exe opens none, and it is
    what start at sign-in runs."""
    scripts, started = _windows(monkeypatch, tmp_path, pythonw=True)
    vault = str(tmp_path / "Zoë Ann" / "v.vault")
    cli._start_status_bar_app(vault)
    (argv, kwargs), = started
    assert argv == [str(scripts / "pythonw.exe"), "-m", "compartment.cli",
                    "--vault", vault, "tray"]
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_windows_without_pythonw_still_starts_the_console_script(
        monkeypatch, tmp_path, capsys):
    scripts, started = _windows(monkeypatch, tmp_path, pythonw=False)
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    (argv, _), = started
    assert argv[0] == str(scripts / "compartment.exe")
