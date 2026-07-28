"""Installing connects what is here.

The install used to end at a panel of buttons nobody had pressed, so the
first thing a new user saw was a row of agents reporting that none of them
were connected, on a machine where every one of them could have been. These
cover the detection that decides who gets wired, and the guarantee that one
agent failing cannot take the install down with it.
"""
import os
import sys
import types

import pytest

from compartment import cli


# ------------------------------------------------------------------ detection

@pytest.fixture()
def nothing_installed(monkeypatch, tmp_path):
    """A machine with no agents: no CLIs on PATH, no config directories."""
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    monkeypatch.setattr(cli.claude_desktop, "present", lambda *a, **k: False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    return tmp_path


def test_nothing_installed_means_nothing_detected(nothing_installed):
    assert [t for t in ("claude", "hermes", "openclaw")
            if cli.agent_present(t)] == []


def test_a_cli_on_path_counts(nothing_installed, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k:
                        "/usr/local/bin/hermes" if name == "hermes" else None)
    assert cli.agent_present("hermes") is True
    assert cli.agent_present("openclaw") is False


def test_a_config_directory_counts_without_a_cli(nothing_installed):
    (nothing_installed / ".openclaw").mkdir()
    assert cli.agent_present("openclaw") is True


def test_claude_desktop_alone_counts_as_claude(nothing_installed, monkeypatch):
    """A Desktop-only machine has no `claude` on PATH, and is still a machine
    with Claude on it."""
    monkeypatch.setattr(cli.claude_desktop, "present", lambda *a, **k: True)
    assert cli.agent_present("claude") is True


def test_hermes_home_env_is_honoured(nothing_installed, monkeypatch, tmp_path):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(elsewhere))
    assert cli.agent_present("hermes") is True


# ---------------------------------------------------------------- connecting

def test_every_installed_agent_is_connected(nothing_installed, monkeypatch,
                                            capsys):
    monkeypatch.setattr(cli, "agent_present",
                        lambda t: t in ("claude", "hermes"))
    seen = []
    monkeypatch.setattr(cli, "cmd_integrate",
                        lambda args: seen.append(args.target))

    done = cli.connect_present_agents("/v/memory.vault")

    assert seen == ["claude", "hermes"]
    assert done == ["Claude", "Hermes"]


def test_an_agent_that_is_not_here_is_left_alone(nothing_installed,
                                                 monkeypatch):
    monkeypatch.setattr(cli, "agent_present", lambda t: t == "openclaw")
    seen = []
    monkeypatch.setattr(cli, "cmd_integrate",
                        lambda args: seen.append(args.target))
    assert cli.connect_present_agents("/v") == ["OpenClaw"]
    assert seen == ["openclaw"]


def test_no_agents_says_so_and_does_not_fail(nothing_installed, capsys):
    assert cli.connect_present_agents("/v") == []
    assert "No agent found" in capsys.readouterr().out


def test_one_agent_failing_does_not_stop_the_others(nothing_installed,
                                                    monkeypatch, capsys):
    """An install that dies half way through wiring leaves a vault with some
    agents connected and no message saying which."""
    monkeypatch.setattr(cli, "agent_present", lambda t: True)

    def flaky(args):
        if args.target == "hermes":
            raise RuntimeError("hermes venv is broken")

    monkeypatch.setattr(cli, "cmd_integrate", flaky)
    done = cli.connect_present_agents("/v")

    assert done == ["Claude", "OpenClaw"]
    assert "could not connect Hermes" in capsys.readouterr().out


def test_a_die_inside_integrate_does_not_end_the_install(nothing_installed,
                                                         monkeypatch, capsys):
    monkeypatch.setattr(cli, "agent_present", lambda t: t == "claude")

    def dies(args):
        raise SystemExit("something went wrong")

    monkeypatch.setattr(cli, "cmd_integrate", dies)
    assert cli.connect_present_agents("/v") == []
    assert "could not connect Claude" in capsys.readouterr().out


def test_the_vault_path_is_passed_through(nothing_installed, monkeypatch):
    monkeypatch.setattr(cli, "agent_present", lambda t: t == "claude")
    got = {}
    monkeypatch.setattr(cli, "cmd_integrate",
                        lambda args: got.update(vault=args.vault,
                                                no_import=args.no_import,
                                                no_hooks=args.no_hooks))
    cli.connect_present_agents("/somewhere/memory.vault")
    assert got == {"vault": "/somewhere/memory.vault",
                   "no_import": False, "no_hooks": False}
