"""On Windows, nothing the panel starts opens a console window.

The tray app runs under pythonw.exe, which has no console. Every CLI it
starts is a console program, and a console program started without
CREATE_NO_WINDOW from a process with no console gets a window of its own:
a flash for each click on Unlock, Lock, Create, Change password or Connect,
and, for Dashboard, a window that stayed open for as long as the dashboard
ran, where closing it stopped the dashboard. Only the vault view and the
JSON status calls had the flag.
"""
from __future__ import annotations

import io
import subprocess

import pytest

from compartment import menubar

CREATE_NO_WINDOW = 0x08000000


@pytest.fixture()
def children(monkeypatch, tmp_path):
    """Every child the panel starts, with the keyword arguments it got."""
    seen: list[tuple[list, dict]] = []

    def run(argv, **kw):
        seen.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    class Proc:
        def __init__(self, argv, **kw):
            seen.append((list(argv), kw))
            self.stdout = io.StringIO(
                "Compartment dashboard: http://127.0.0.1:1/t/\n")
            self.stdin = io.StringIO()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(menubar.sys, "platform", "win32")
    monkeypatch.setattr(menubar.subprocess, "run", run)
    monkeypatch.setattr(menubar.subprocess, "Popen", Proc)
    monkeypatch.setattr(menubar, "user_path", lambda: "")
    monkeypatch.setattr(menubar, "agent_present", lambda target: True)
    monkeypatch.setattr(menubar, "integration_status",
                        lambda vault: {"claude": True})
    yield seen
    menubar._DASH.update(proc=None, url=None)


def _flags(kw: dict) -> int:
    return int(kw.get("creationflags", 0))


@pytest.mark.parametrize("click", [
    lambda v: menubar.unlock_vault(v, "pw"),
    lambda v: menubar.lock_vault(v),
    lambda v: menubar.create_vault(v + ".new", "pw", "pw"),
    lambda v: menubar.change_passphrase(v, "pw", "pw"),
    lambda v: menubar.integrate(v, "claude"),
    lambda v: menubar.open_dashboard(v),
], ids=["unlock", "lock", "create", "change-password", "connect",
        "dashboard"])
def test_every_button_starts_its_cli_without_a_console(children, tmp_path,
                                                       click):
    click(str(tmp_path / "memory.vault"))
    cli = [(argv, kw) for argv, kw in children if "compartment.cli" in argv]
    assert cli, "the click ran the CLI"
    for argv, kw in cli:
        assert _flags(kw) & CREATE_NO_WINDOW, argv


def test_the_vault_view_and_status_calls_keep_theirs(children, tmp_path):
    vault = str(tmp_path / "memory.vault")
    menubar.ViewProcess(vault)._start()
    menubar._json_run(["x"])
    assert all(_flags(kw) & CREATE_NO_WINDOW for _argv, kw in children)


def test_elsewhere_no_windows_flag_is_passed(monkeypatch):
    """creationflags is Windows-only; passing it on POSIX raises."""
    monkeypatch.setattr(menubar.sys, "platform", "darwin")
    assert menubar._no_console() == {}
    monkeypatch.setattr(menubar.sys, "platform", "linux")
    assert menubar._no_console() == {}
