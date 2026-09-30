"""An upgrade reaches the copies of the instructions an earlier install wrote.

The MCP handshake speaks with whatever code is installed, but three things an
earlier `integrate` wrote into the agents' own files are copies: the fenced
block in CLAUDE.md, the /compartmentalize skill and the Hermes provider
plugin. Left alone they keep last version's wording, and an agent reading
both is told two different things about what to store.

`compartment integrate --refresh` updates exactly those, only where they are
already installed, and touches nothing else - in particular not the capture
hook, which re-running `integrate claude` would reinstall for a user who had
switched it off. `compartment update` runs it with the new build.
"""
import types

import pytest

from compartment import agent_skill, claude_hooks, cli, gate


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads USERPROFILE on Windows and never looks at HOME, so a
    # HOME override alone left the refresh looking in the runner's real home.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(agent_skill, "SKILL_TARGETS", {
        "claude": (None, tmp_path / ".claude"),
        "hermes": ("HERMES_HOME", tmp_path / ".hermes"),
        "openclaw": ("OPENCLAW_HOME", tmp_path / ".openclaw"),
    })
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_MD", str(tmp_path / ".claude" / "CLAUDE.md"))
    return tmp_path


OLD_BLOCK = (cli._CLAUDE_MD_BEGIN + "\ncompartment is your persistent, "
             "encrypted memory. The moment information worth keeping appears "
             "that is not common public knowledge - save it.\n"
             + cli._CLAUDE_MD_END)


def test_an_old_block_is_updated_and_the_users_text_left_alone(homes):
    md = homes / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text("# My notes\nkeep me\n\n" + OLD_BLOCK + "\n\nand me\n",
                  encoding="utf-8")
    done = cli._refresh_installed()
    text = md.read_text(encoding="utf-8")
    assert "keep me" in text and "and me" in text
    assert " ".join(gate.STORE_RULE.split()) in " ".join(text.split())
    assert "The moment information worth keeping" not in text
    assert text.count(cli._CLAUDE_MD_BEGIN) == 1
    assert done and "CLAUDE.md" in done[0]
    assert cli._refresh_installed() == []          # and then it is current


def test_a_claude_md_without_the_block_is_not_touched(homes):
    md = homes / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text("# only my own notes\n", encoding="utf-8")
    before = md.read_bytes()
    assert cli._refresh_installed() == []
    assert md.read_bytes() == before


def test_an_installed_skill_is_updated_and_a_missing_one_not_created(homes):
    old = homes / ".claude" / "skills" / "compartmentalize" / "SKILL.md"
    old.parent.mkdir(parents=True)
    old.write_text("last version's skill", encoding="utf-8")
    done = cli._refresh_installed()
    assert old.read_bytes() == agent_skill.source().read_bytes()
    kept = old.with_name("SKILL.md" + agent_skill.BACKUP_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "last version's skill"
    assert any("skill" in d for d in done)
    # never installed for hermes or openclaw, so not installed now either
    assert not (homes / ".hermes").exists()
    assert not (homes / ".openclaw").exists()


def test_the_hermes_plugin_is_updated_only_where_installed(homes):
    assert cli._refresh_installed() == []
    assert not (homes / ".hermes").exists()        # nothing created
    plug = homes / ".hermes" / "plugins" / "compartment"
    plug.mkdir(parents=True)
    (plug / "plugin.yaml").write_text("version: 4.10.1\n", encoding="utf-8")
    (plug / "__init__.py").write_text("# last version\n", encoding="utf-8")
    done = cli._refresh_installed()
    src = cli._data_dir() / "hermes-plugin"
    for f in ("__init__.py", "plugin.yaml"):
        assert (plug / f).read_bytes() == (src / f).read_bytes()
    assert any("Hermes" in d for d in done)


def test_refresh_never_installs_a_capture_hook(homes, monkeypatch):
    """The user's own switch. `integrate claude` would put the hook back;
    a refresh must not."""
    calls = []
    monkeypatch.setattr(claude_hooks, "install",
                        lambda *a, **k: calls.append(1))
    md = homes / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text(OLD_BLOCK + "\n", encoding="utf-8")
    cli._refresh_installed()
    cli.main(["integrate", "--refresh"])
    assert calls == []


def test_the_command_says_what_it_did(homes, capsys):
    cli.main(["integrate", "--refresh"])
    assert "already current" in capsys.readouterr().out
    md = homes / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text(OLD_BLOCK + "\n", encoding="utf-8")
    cli.main(["integrate", "--refresh"])
    assert "updated the compartment block" in capsys.readouterr().out


def test_update_refreshes_with_the_new_build(monkeypatch, capsys):
    """The process running `update` is the old version; the refresh has to be
    the new one's, so it is a separate command run after the upgrade."""
    ran = []

    def fake_run(cmd, *a, **k):
        ran.append(list(cmd))
        out = ""
        if cmd[-2:] == ["integrate", "--refresh"]:
            out = "  ✓ updated the compartment block in CLAUDE.md\n"
        elif cmd[-1:] == ["--version"]:
            out = "compartment 9.9.9\n"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_install_kind",
                        lambda: ("pip", ["pip", "install", "-U", "compartment"]))
    from compartment import embed_daemon
    monkeypatch.setattr(embed_daemon, "stop", lambda *a, **k: {})
    cli.cmd_update(types.SimpleNamespace(source=False, no_app=True,
                                         vault="unused"))
    assert ran[0] == ["pip", "install", "-U", "compartment"]
    assert any(c[-2:] == ["integrate", "--refresh"] for c in ran)
    assert "updated the compartment block" in capsys.readouterr().out
