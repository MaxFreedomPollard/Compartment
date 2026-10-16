"""What `compartment uninstall` takes back, and what it leaves.

Everything it reads or rewrites here is under tmp_path: the home directory,
Claude Code's settings and CLAUDE.md, and every agent's own home. Nothing it
would run or stop is real: the status bar app is absent and subprocess.run
is a stub that records the commands uninstall tried to run."""
import subprocess
import types

import pytest

from compartment import agent_skill, claude_hooks, cli, menubar


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # Path.home() reads USERPROFILE on Windows and never HOME, so the method
    # itself points here; APPDATA is where the Windows clients keep theirs.
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("CLAUDE_MD", str(home / ".claude" / "CLAUDE.md"))
    monkeypatch.setattr(claude_hooks, "SETTINGS",
                        home / ".claude" / "settings.json")
    monkeypatch.setattr(agent_skill, "SKILL_TARGETS", {
        "claude": (None, home / ".claude"),
        "hermes": ("HERMES_HOME", home / ".hermes"),
        "openclaw": ("OPENCLAW_HOME", home / ".openclaw"),
    })
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("OPENCLAW_HOME", str(home / ".openclaw"))
    monkeypatch.setattr(cli, "_tray_app",
                        lambda: (_ for _ in ()).throw(RuntimeError("no app")))
    monkeypatch.setattr(menubar, "_is_generated", lambda path: False)
    ran = []

    def run(argv, **kwargs):
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    return types.SimpleNamespace(
        home=home, ran=ran, vault=str(home / ".compartment" / "memory.vault"))


def _uninstall(machine):
    cli.cmd_uninstall(types.SimpleNamespace(purge=False, vault=machine.vault))


def test_an_edited_skill_is_kept_and_named(machine, capsys):
    """Uninstalling deleted a /compartmentalize skill the user had rewritten,
    with no backup and nothing said."""
    for target in agent_skill.SKILL_TARGETS:
        agent_skill.install(target)
    edited = agent_skill.skill_path("claude")
    edited.write_text("my own sweep instructions, tuned over weeks\n",
                      encoding="utf-8")
    _uninstall(machine)
    out = capsys.readouterr().out
    assert edited.read_text(encoding="utf-8").startswith("my own sweep")
    assert "/compartmentalize skill kept in claude" in out, out
    assert str(edited) in out, out
    for target in ("hermes", "openclaw"):
        assert not agent_skill.is_installed(target)
        assert f"/compartmentalize skill removed from {target}" in out, out
