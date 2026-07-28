"""The app has to be able to find the agent it is wiring.

A status bar app started at login inherits launchd's PATH, which is
/usr/bin:/bin:/usr/sbin:/sbin. `claude` installs into ~/.local/bin and
Homebrew into /opt/homebrew/bin, so the Connect button ran an `integrate`
that could not see the tool it was there to connect: it skipped the
registration, said the agent was not installed, and left the user looking at
a button that appeared to do nothing.
"""
import json
import os
import subprocess
import sys

import pytest

from compartment import menubar

LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture(autouse=True)
def _fresh_path_cache():
    menubar._USER_PATH = None
    yield
    menubar._USER_PATH = None


@pytest.fixture()
def minimal_env(monkeypatch):
    """Exactly what a login item gets, and no login shell to ask."""
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no shell")))


# ------------------------------------------------------------------ the PATH

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX login item PATH")
def test_the_users_own_bin_directory_is_reachable(minimal_env):
    """~/.local/bin is where `claude` installs itself, and it is exactly what
    launchd leaves out."""
    assert str(menubar.Path.home() / ".local" / "bin") in \
        menubar.user_path().split(os.pathsep)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX login item PATH")
def test_homebrew_is_reachable(minimal_env):
    assert "/opt/homebrew/bin" in menubar.user_path().split(os.pathsep)


def test_what_the_process_already_had_is_never_dropped(minimal_env):
    for p in LAUNCHD_PATH.split(os.pathsep):
        assert p in menubar.user_path().split(os.pathsep)


def test_the_login_shell_is_asked_first(monkeypatch):
    """Only the user's shell knows the user's PATH. The fixed list is a
    floor, not the answer."""
    if sys.platform == "win32":
        pytest.skip("POSIX login shell")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PATH", LAUNCHD_PATH)

    class R:
        stdout = "/opt/only/here:/usr/bin"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    parts = menubar.user_path().split(os.pathsep)
    assert parts[0] == "/opt/only/here"


def test_no_directory_is_listed_twice(minimal_env):
    parts = menubar.user_path().split(os.pathsep)
    assert len(parts) == len(set(parts))


def test_it_is_worked_out_once(monkeypatch):
    calls = []

    class R:
        stdout = "/usr/bin"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (calls.append(1), R())[1])
    menubar.user_path()
    menubar.user_path()
    assert len(calls) <= 1


def test_a_shell_that_hangs_does_not_hang_the_app(monkeypatch):
    """A timeout must fall back, not propagate: the panel would never open."""
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("sh", 15)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    assert "/usr/bin" in menubar.user_path()


def test_every_cli_call_gets_that_path(monkeypatch):
    seen = {}

    class R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake(args, **kw):
        seen.update(kw)
        return R()
    monkeypatch.setattr(subprocess, "run", fake)
    menubar._run(["/bin/true"])
    assert seen["env"]["PATH"] == menubar.user_path()


def test_the_hook_records_a_findable_compartment(monkeypatch, tmp_path):
    """The hook command is written into the agent's config and run later by
    the agent, so a bare name that only resolves in some shells is a hook
    that silently stops firing. The console script beside the interpreter is
    used when there is one; this covers the case where there is not, which
    is where the login item's PATH used to leave a bare "compartment".
    """
    # Windows resolves a bare name through PATHEXT, so a file with no
    # extension is not an executable there.
    exe = tmp_path / "bin" / ("compartment.exe" if sys.platform == "win32"
                              else "compartment")
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setattr(menubar.sys, "prefix", str(empty))
    monkeypatch.setattr(menubar.sys, "executable", str(empty / "python3"))
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(menubar, "user_path", lambda: str(exe.parent))
    # Windows hands back the extension in the casing PATHEXT uses (.EXE).
    assert menubar.compartment_bin().lower() == str(exe).lower()


def test_without_the_path_fix_it_would_have_written_a_bare_name(monkeypatch,
                                                                tmp_path):
    """What the login item used to do: nothing on launchd's PATH is called
    compartment, so the hook got the unqualified word."""
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setattr(menubar.sys, "prefix", str(empty))
    monkeypatch.setattr(menubar.sys, "executable", str(empty / "python3"))
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(menubar, "user_path", lambda: LAUNCHD_PATH)
    assert menubar.compartment_bin() == "compartment"


# --------------------------------------------------------- already connected

@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(menubar.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setattr(menubar, "integration_status",
                        menubar.integration_status)
    return tmp_path


def test_a_clean_machine_is_connected_to_nothing(fake_home, monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    assert menubar.integration_status("/v") == {
        "claude": False, "hermes": False, "openclaw": False}


def test_claude_counts_as_connected_once_it_is_registered(fake_home):
    (fake_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"compartment": {"command": "compartment"}}}))
    assert menubar.integration_status("/v")["claude"] is True


def test_hermes_counts_as_connected_once_the_plugin_is_there(fake_home,
                                                             monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    plug = fake_home / ".hermes" / "plugins" / "compartment"
    plug.mkdir(parents=True)
    (plug / "plugin.yaml").write_text("name: compartment\n")
    st = menubar.integration_status("/v")
    assert st["hermes"] is True and st["claude"] is False


def test_openclaw_counts_as_connected_once_it_is_in_the_config(fake_home,
                                                              monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    d = fake_home / ".openclaw"
    d.mkdir()
    (d / "openclaw.json").write_text(
        json.dumps({"mcpServers": {"compartment": {}}}))
    assert menubar.integration_status("/v")["openclaw"] is True


def test_a_broken_config_file_is_not_a_crash(fake_home, monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    (fake_home / ".claude.json").write_text("{ this is not json")
    assert menubar.integration_status("/v")["claude"] is False


def test_clicking_again_says_it_was_already_connected(monkeypatch):
    monkeypatch.setattr(menubar, "integration_status",
                        lambda v: {"claude": True})
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: (0, "registered."))
    ok, msg = menubar.integrate("/v", "claude")
    assert ok is True
    assert "already connected" in msg


def test_the_first_click_says_it_connected(monkeypatch):
    monkeypatch.setattr(menubar, "integration_status",
                        lambda v: {"claude": False})
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: (0, "registered."))
    ok, msg = menubar.integrate("/v", "claude")
    assert ok is True
    assert "already" not in msg and "connected" in msg


def test_a_missing_agent_says_so_without_claiming_it_is_uninstallable(
        monkeypatch):
    monkeypatch.setattr(menubar, "integration_status",
                        lambda v: {"hermes": False})
    monkeypatch.setattr(menubar, "_run",
                        lambda *a, **k: (0, "hermes not found; finish with…"))
    ok, msg = menubar.integrate("/v", "hermes")
    assert ok is True
    assert "Could not find Hermes" in msg and "click again" in msg


# ---------------------------------------------------------------- the panel

def test_the_panel_ticks_an_agent_that_is_already_connected():
    from compartment import systray
    state = {"vault": "/v", "exists": True, "locked": False, "records": 1,
             "organic": 1, "recent": [], "error": None,
             "integrations": {"claude": True, "hermes": False,
                              "openclaw": False},
             "settings": {"capture_hook": True, "search_starter_facts": True,
                          "auto_lock_minutes": 30}}
    rows = systray.panel_rows(state)
    assert ("connect:claude", "Claude ✓") in rows
    assert ("connect:hermes", "Hermes") in rows


def test_the_panel_knows_before_anything_is_clicked(tmp_path, monkeypatch):
    """fetch_state carries it, so the ticks are right the moment the panel
    opens rather than only after a click."""
    monkeypatch.setattr(menubar, "integration_status",
                        lambda v: {"claude": True, "hermes": False,
                                   "openclaw": False})
    st = menubar.fetch_state(str(tmp_path / "nope.vault"))
    assert st["integrations"]["claude"] is True
