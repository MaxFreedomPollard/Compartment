"""Claude Desktop on Windows is an MSIX package now, from the Store or from
the direct download, and Windows keeps a packaged app's %APPDATA% writes in
the package's own LocalCache. The app reads that private copy first, so a
config written to the real %APPDATA%\\Claude is one it never sees."""
import json

import pytest

from compartment import claude_desktop

DIRECT, STORE = claude_desktop.MSIX_FAMILIES
ARGS = ["--vault", r"C:\Users\ana\.compartment\memory.vault", "--caller",
        "claude-desktop", "serve"]


@pytest.fixture()
def windows(monkeypatch, tmp_path):
    """A Windows profile under tmp_path: %APPDATA% and %LOCALAPPDATA%."""
    monkeypatch.setattr(claude_desktop, "_windows", lambda: True,
                        raising=False)
    monkeypatch.setattr(claude_desktop.sys, "platform", "win32")
    roaming = tmp_path / "AppData" / "Roaming"
    local = tmp_path / "AppData" / "Local"
    roaming.mkdir(parents=True)
    local.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    class Profile:
        real = roaming / "Claude" / claude_desktop.FILENAME

        @staticmethod
        def package(family):
            (local / "Packages" / family).mkdir(parents=True, exist_ok=True)
            return (local / "Packages" / family / "LocalCache" / "Roaming"
                    / "Claude" / claude_desktop.FILENAME)

        @staticmethod
        def write(p, servers):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    return Profile


def test_an_msix_install_is_written_where_the_app_reads(
        windows, real_claude_desktop_config_path):
    """The package had saved its settings to its private copy, so the app
    no longer read %APPDATA%\\Claude, and that is where the entry went:
    `integrate claude` reported Claude Desktop connected, and it never saw
    the server."""
    private = windows.package(DIRECT)
    windows.write(private, {"theirs": {"command": "npx"}})
    windows.write(windows.real, {"stale": {"command": "old"}})
    stale = windows.real.read_text(encoding="utf-8")

    p = real_claude_desktop_config_path()
    assert p == private
    claude_desktop.register(r"C:\x\compartment.exe", ARGS, path=p)

    servers = json.loads(private.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"theirs", "compartment"}
    assert windows.real.read_text(encoding="utf-8") == stale


def test_the_store_build_is_found_too(windows,
                                      real_claude_desktop_config_path):
    private = windows.package(STORE)
    windows.write(private, {})
    assert real_claude_desktop_config_path() == private


def test_a_package_that_has_not_saved_yet_reads_the_real_file(
        windows, real_claude_desktop_config_path):
    """Until the package saves its own copy, Windows shows it the real file;
    a private file holding only our server would hide every server in
    it."""
    windows.package(DIRECT)
    windows.write(windows.real, {"theirs": {"command": "npx"}})
    assert real_claude_desktop_config_path() == windows.real


def test_a_package_with_no_config_anywhere_gets_its_private_one(
        windows, real_claude_desktop_config_path):
    private = windows.package(DIRECT)
    assert real_claude_desktop_config_path() == private


def test_of_two_packages_the_one_saved_last_is_written(
        windows, real_claude_desktop_config_path):
    import os
    old, new = windows.package(DIRECT), windows.package(STORE)
    windows.write(old, {})
    windows.write(new, {})
    os.utime(old, (1_000_000, 1_000_000))
    assert real_claude_desktop_config_path() == new


def test_without_a_package_the_regular_install_uses_appdata(
        windows, real_claude_desktop_config_path):
    assert real_claude_desktop_config_path() == windows.real


@pytest.mark.parametrize("installed", ["direct", "store", "squirrel"])
def test_an_install_that_was_never_opened_is_present(windows, installed,
                                                     tmp_path):
    """A package that has never run has no %APPDATA%\\Claude, so it was
    reported as not installed and `integrate claude` skipped it."""
    assert claude_desktop.present() is False
    if installed == "squirrel":
        (tmp_path / "AppData" / "Local" / "AnthropicClaude").mkdir()
    else:
        windows.package(DIRECT if installed == "direct" else STORE)
    assert claude_desktop.present() is True
