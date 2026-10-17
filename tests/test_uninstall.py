"""What `compartment uninstall` takes back, and what it leaves.

Everything it reads or rewrites here is under tmp_path: the home directory,
Claude Code's settings and CLAUDE.md, and every agent's own home. Nothing it
would run or stop is real: the status bar app is absent and subprocess.run
is a stub that records the commands uninstall tried to run."""
import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from compartment import (agent_skill, claude_desktop, claude_hooks, cli,
                         clients, menubar)


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


def _wire_everything(machine):
    """What integrate leaves on a machine with every agent on it: the MCP
    registrations, the CLAUDE.md block and the Hermes provider plugin,
    written by the code integrate uses, with nothing run."""
    exe, vp = clients.executable(), machine.vault
    claude_desktop.register(exe, ["--vault", vp, "--caller",
                                  "claude-desktop", "serve"])
    cursor, codex = clients.CLIENTS["cursor"], clients.CLIENTS["codex"]
    cursor.config_path().parent.mkdir(parents=True)
    cursor.config_path().write_text(
        json.dumps({"mcpServers": {"theirs": {"command": "x"}}}) + "\n",
        encoding="utf-8")
    for client in (cursor, codex):
        assert clients.register(client, vp)["written"]
    openclaw = machine.home / ".openclaw" / "openclaw.json"
    openclaw.parent.mkdir(parents=True)
    openclaw.write_text(json.dumps({"gateway": {"port": 18789}}),
                        encoding="utf-8")
    cli._integrate_target(types.SimpleNamespace(target="openclaw", vault=vp))
    md = machine.home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# My notes\nkeep me\n", encoding="utf-8")
    cli._write_managed_claude_md()
    plugin = machine.home / ".hermes" / "plugins" / "compartment"
    plugin.mkdir(parents=True)
    for f in ("__init__.py", "plugin.yaml"):
        shutil.copy2(cli._data_dir() / "hermes-plugin" / f, plugin / f)
    (plugin / "__pycache__").mkdir()        # Hermes importing it leaves one
    (plugin / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"\0")
    return types.SimpleNamespace(cursor=cursor, codex=codex,
                                 openclaw=openclaw, md=md, plugin=plugin)


def test_uninstall_takes_back_every_registration_integrate_wrote(machine,
                                                                  capsys):
    """Uninstall took out the Claude Code registration, the capture hook and
    the skills, and left Claude Desktop, OpenClaw, every client `integrate
    --all` wired, the CLAUDE.md block and the Hermes provider plugin, all
    naming a program about to be removed."""
    w = _wire_everything(machine)
    _uninstall(machine)
    out = capsys.readouterr().out

    assert not claude_desktop.is_registered()
    assert not clients.is_registered(w.cursor)
    assert not clients.is_registered(w.codex)
    assert json.loads(w.cursor.config_path().read_text(encoding="utf-8")) == {
        "mcpServers": {"theirs": {"command": "x"}}}
    assert json.loads(w.openclaw.read_text(encoding="utf-8")) == {
        "gateway": {"port": 18789}, "mcpServers": {}}
    assert w.md.read_text(encoding="utf-8") == "# My notes\nkeep me\n"
    assert not w.plugin.exists()
    for said in ("Claude Desktop registration removed",
                 "removed from Cursor", "removed from Codex CLI",
                 "OpenClaw registration removed",
                 f"compartment block removed from {w.md}",
                 "Hermes provider plugin removed"):
        assert said in out, out
    assert ["claude", "mcp", "remove", "compartment"] in machine.ran


def test_a_step_that_cannot_finish_says_why_and_the_rest_still_run(machine,
                                                                   capsys):
    w = _wire_everything(machine)
    desktop = claude_desktop.config_path()
    desktop.write_text("{ not json", encoding="utf-8")
    (w.plugin / "__init__.py").write_text("# my own provider tweaks\n",
                                          encoding="utf-8")
    vault = Path(machine.vault)
    vault.parent.mkdir(parents=True)
    vault.write_bytes(b"every memory")
    _uninstall(machine)
    out = capsys.readouterr().out

    assert desktop.read_text(encoding="utf-8") == "{ not json"
    assert "Claude Desktop: " in out and "refusing to touch it" in out, out
    assert (w.plugin / "__init__.py").read_text(
        encoding="utf-8").startswith("# my own")
    assert "Hermes provider plugin kept" in out, out
    assert not clients.is_registered(w.cursor)
    assert cli._CLAUDE_MD_BEGIN not in w.md.read_text(encoding="utf-8")
    assert vault.read_bytes() == b"every memory"


def test_a_hermes_plugin_linked_in_from_elsewhere_is_not_emptied(
        machine, capsys, tmp_path):
    """integrate copies the plugin in, so a plugin directory that is a link
    was put there by the user, and deleting through it would empty the
    place it points at, such as a checkout of this repository."""
    checkout = tmp_path / "checkout" / "hermes-plugin"
    checkout.mkdir(parents=True)
    for f in ("__init__.py", "plugin.yaml"):
        shutil.copy2(cli._data_dir() / "hermes-plugin" / f, checkout / f)
    link = machine.home / ".hermes" / "plugins" / "compartment"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(checkout, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"no symlinks here ({exc})")
    _uninstall(machine)
    out = capsys.readouterr().out
    assert sorted(p.name for p in checkout.iterdir()) == ["__init__.py",
                                                          "plugin.yaml"]
    assert "Hermes provider plugin kept" in out and "is a link" in out, out


@pytest.mark.parametrize("mine", ["", "# My notes\nkeep me\n",
                                  "no newline at the end"])
def test_the_claude_md_block_comes_out_as_it_went_in(machine, mine):
    md = machine.home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    if mine:
        md.write_text(mine, encoding="utf-8")
    cli._write_managed_claude_md()
    assert cli._remove_managed_claude_md() == md
    assert md.read_text(encoding="utf-8") == (
        mine.rstrip() + "\n" if mine else "")


def test_only_the_fenced_block_leaves_claude_md(machine):
    md = machine.home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text("# My notes\nkeep me\n\n" + cli._CLAUDE_MD_BEGIN
                  + "\nlast version's words\n" + cli._CLAUDE_MD_END
                  + "\n\nand me\n", encoding="utf-8")
    cli._remove_managed_claude_md()
    assert md.read_text(encoding="utf-8") == "# My notes\nkeep me\n\nand me\n"

    md.write_text("# no block here\n", encoding="utf-8")
    assert cli._remove_managed_claude_md() is None
    assert md.read_text(encoding="utf-8") == "# no block here\n"
