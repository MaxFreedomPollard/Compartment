"""What `compartment init` does to put the status bar app up: nothing
registered for a panel that cannot draw."""
import sys
import types

from compartment import cli


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
