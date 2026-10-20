"""Opening the dashboard in a browser, on every kind of machine.

`compartment dash` and the panel's Dashboard button open the page
themselves. webbrowser.open alone got headless Linux and WSL wrong: with no
display it falls back to a text browser run in the same terminal and waits
for it, and `dash` did that before serving, so the browser waited on a
server that could not answer until the browser exited.
"""
from __future__ import annotations

import threading
import time

import pytest

from compartment import dash, menubar

URL = "http://127.0.0.1:5555/tok/"


@pytest.fixture()
def linux(monkeypatch):
    monkeypatch.setattr(menubar.sys, "platform", "linux")
    for name in ("BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "WSL_DISTRO_NAME",
                 "WSL_INTEROP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(menubar, "_is_wsl", lambda: False)
    opened, started = [], []
    monkeypatch.setattr(menubar.webbrowser, "open",
                        lambda url, *a, **k: opened.append(url) or True)
    monkeypatch.setattr(menubar.subprocess, "Popen",
                        lambda argv, **k: started.append(argv))
    return opened, started


def test_a_headless_linux_box_opens_nothing(linux):
    """No display: the only browsers webbrowser could find are text ones
    that take over this terminal."""
    opened, started = linux
    assert menubar.open_in_browser(URL) is False
    assert opened == [] and started == []


def test_a_desktop_session_gets_its_browser(linux, monkeypatch):
    opened, _ = linux
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert menubar.open_in_browser(URL) is True
    assert opened == [URL]


def test_browser_is_honoured_even_without_a_display(linux, monkeypatch):
    opened, _ = linux
    monkeypatch.setenv("BROWSER", "w3m")
    assert menubar.open_in_browser(URL) is True
    assert opened == [URL]


@pytest.mark.parametrize("on_path,expected", [
    ({"wslview", "explorer.exe"}, "wslview"),
    ({"explorer.exe"}, "explorer.exe"),
])
def test_wsl_opens_the_windows_browser(linux, monkeypatch, on_path, expected):
    opened, started = linux
    monkeypatch.setattr(menubar, "_is_wsl", lambda: True)
    monkeypatch.setenv("DISPLAY", ":0")          # WSLg: still the Windows one
    monkeypatch.setattr(menubar.shutil, "which",
                        lambda name, *a, **k: (f"/usr/bin/{name}"
                                               if name in on_path else None))
    assert menubar.open_in_browser(URL) is True
    assert started == [[f"/usr/bin/{expected}", URL]]
    assert opened == []


def test_wsl_without_interop_says_it_could_not(linux, monkeypatch):
    opened, started = linux
    monkeypatch.setattr(menubar, "_is_wsl", lambda: True)
    monkeypatch.setattr(menubar.shutil, "which", lambda *a, **k: None)
    assert menubar.open_in_browser(URL) is False
    assert opened == [] and started == []


def test_wsl_is_recognised(monkeypatch, tmp_path):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert menubar._is_wsl() is True


def test_a_browser_that_fails_never_raises(monkeypatch):
    monkeypatch.setattr(menubar.sys, "platform", "darwin")

    def boom(*a, **k):
        raise OSError("LSOpenURLsWithRole() failed")

    monkeypatch.setattr(menubar.webbrowser, "open", boom)
    assert menubar.open_in_browser(URL) is False


class _FakeServer:
    def __init__(self):
        self.served = threading.Event()

    def serve_forever(self):
        self.served.set()

    def server_close(self):
        pass


def test_dash_serves_while_the_browser_is_still_open(monkeypatch, capsys,
                                                     tmp_path):
    """A text browser in the foreground used to be waited for before the
    server started, so it waited on a page nobody would serve."""
    server = _FakeServer()
    release = threading.Event()
    seen = {}

    def foreground_browser(url, *a, **k):
        seen["served_before_return"] = server.served.wait(5)
        release.wait(5)
        return True

    monkeypatch.setattr(dash, "start", lambda ref: (server, URL))
    monkeypatch.setattr(menubar, "open_in_browser", foreground_browser)
    monkeypatch.setattr(menubar.webbrowser, "open", foreground_browser)
    began = time.monotonic()
    dash.run(str(tmp_path / "v.vault"), vault=None)
    assert server.served.is_set()
    assert time.monotonic() - began < 4, "serving waited on the browser"
    release.set()
    deadline = time.monotonic() + 5
    while "served_before_return" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen.get("served_before_return") is True


def test_dash_says_how_to_reach_the_page_when_nothing_opens(monkeypatch,
                                                           capsys, tmp_path):
    server = _FakeServer()
    monkeypatch.setattr(dash, "start", lambda ref: (server, URL))
    monkeypatch.setattr(menubar, "open_in_browser", lambda url: False)
    dash.run(str(tmp_path / "v.vault"), vault=None)
    deadline = time.monotonic() + 5
    out = ""
    while "no browser" not in out and time.monotonic() < deadline:
        out += capsys.readouterr().out
        time.sleep(0.01)
    assert out.splitlines()[0] == f"Compartment dashboard: {URL}"
    assert "open the address above" in out
    assert "ssh -L 5555:127.0.0.1:5555" in out
