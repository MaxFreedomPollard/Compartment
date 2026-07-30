"""Connecting an agent is a button, on both platforms.

`compartment integrate <agent>` is the step people miss: the vault exists,
the icon is in the menu bar, and nothing is using either because the second
command was never run. The panel offers it directly, and these tests hold the
two front ends to the same list of agents as the CLI.
"""

import pytest

from compartment import cli, clients, menubar, systray


def _state(**over):
    st = {"vault": "/v", "exists": True, "locked": False, "records": 6665,
          "organic": 0, "recent": [], "error": None,
          "settings": {"capture_hook": True, "search_starter_facts": True,
                       "auto_lock_minutes": 30}}
    st.update(over)
    return st


# ------------------------------------------------------------- the agent list

@pytest.mark.parametrize("target,_name", menubar.INTEGRATION_TARGETS)
def test_every_button_names_a_target_the_cli_accepts(monkeypatch, target,
                                                     _name):
    """A button for an agent the CLI rejects is a button that cannot work.
    Goes through the real parser, which is the only thing that can say."""
    seen = {}
    monkeypatch.setattr(cli, "cmd_integrate",
                        lambda args: seen.update(vars(args)))
    cli.main(["integrate", target])
    assert seen["target"] == target


def test_the_cli_accepts_every_button_and_every_known_client(monkeypatch,
                                                             capsys):
    """The other direction: a button the CLI rejects is a button that cannot
    work, and a client `--list` advertises that the CLI rejects is a lie.
    argparse names every choice it accepts when it rejects one, so ask it.

    Containment is one-way now. The panel offers the three agents that get
    the deep treatment - a skill file, a hook, an import - while the CLI
    accepts every MCP client as well. Thirty buttons would not fit in a
    popover, so the panel is a subset on purpose.
    """
    monkeypatch.setattr(cli, "cmd_integrate", lambda args: None)
    with pytest.raises(SystemExit):
        cli.main(["integrate", "no-such-agent"])
    err = capsys.readouterr().err
    # Python 3.11 prints (choose from 'a', 'b'); 3.12 dropped the quotes.
    # Read it in a way that survives both, and the next change to it.
    tail = err.split("choose from")[-1].split(")")[0]
    choices = {c.strip().strip("'\"") for c in tail.split(",") if c.strip()}
    assert {t for t, _ in menubar.INTEGRATION_TARGETS} <= choices
    assert set(clients.ALIASES) <= choices


def test_the_agents_are_offered_in_a_fixed_order():
    assert [t for t, _ in menubar.INTEGRATION_TARGETS] == \
        ["claude", "hermes", "openclaw"]


def test_every_agent_has_a_display_name():
    for target, name in menubar.INTEGRATION_TARGETS:
        assert name and name.lower().replace(" ", "") == target


# ---------------------------------------------------------------- the action

def test_an_unknown_target_never_runs_anything(monkeypatch):
    called = []
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: called.append(a))
    ok, msg = menubar.integrate("/v", "notanagent")
    assert ok is False and "notanagent" in msg
    assert called == []


def test_it_runs_the_documented_command(monkeypatch):
    seen = {}

    def fake_run(args, timeout=60):
        seen["args"] = args
        return 0, "registered."
    monkeypatch.setattr(menubar, "_run", fake_run)
    ok, _ = menubar.integrate("/path/to/v.vault", "claude")
    assert ok is True
    assert seen["args"][-3:] == ["v.vault", "integrate", "claude"] or \
        seen["args"][-2:] == ["integrate", "claude"]
    assert "--vault" in seen["args"]
    assert "/path/to/v.vault" in seen["args"]


def test_success_tells_the_user_to_restart_the_agent(monkeypatch):
    monkeypatch.setattr(menubar, "integration_status", lambda v: {})
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: (0, "registered."))
    ok, msg = menubar.integrate("/v", "claude")
    assert ok is True
    assert "Claude" in msg and "estart" in msg


def test_a_missing_agent_is_reported_as_normal_not_as_failure(monkeypatch):
    """Not having Hermes installed is not an error, and must not read as
    one: the wiring that can be done is still done."""
    monkeypatch.setattr(menubar, "integration_status", lambda v: {})
    monkeypatch.setattr(menubar, "_run",
                        lambda *a, **k: (0, "hermes not found; finish with…"))
    ok, msg = menubar.integrate("/v", "hermes")
    assert ok is True
    assert "Could not find Hermes" in msg


def test_a_real_failure_surfaces_its_reason(monkeypatch):
    monkeypatch.setattr(menubar, "_run",
                        lambda *a, **k: (1, "permission denied writing config"))
    ok, msg = menubar.integrate("/v", "claude")
    assert ok is False
    assert "permission denied" in msg


def test_a_failure_with_no_output_still_says_something(monkeypatch):
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: (1, ""))
    ok, msg = menubar.integrate("/v", "openclaw")
    assert ok is False and msg


def test_wiring_gets_a_longer_leash_than_a_status_call(monkeypatch):
    """Installing into the Hermes venv can take a while; a 60 s default would
    time out mid-pip and leave the agent half-wired."""
    seen = {}

    def fake_run(args, timeout=60):
        seen["timeout"] = timeout
        return 0, "ok"
    monkeypatch.setattr(menubar, "_run", fake_run)
    menubar.integrate("/v", "hermes")
    assert seen["timeout"] >= 300


# ----------------------------------------------------------------- the panel

def test_the_tray_panel_has_a_button_per_agent():
    rows = systray.panel_rows(_state())
    connects = [text for kind, text in rows if kind.startswith("connect:")]
    assert connects == ["Claude", "Hermes", "OpenClaw"]


def test_the_tray_panel_heads_the_section():
    rows = systray.panel_rows(_state())
    assert ("heading", "CONNECT AN AGENT") in rows


def test_the_panel_carries_no_standing_explanation():
    """The heading and the buttons say what this is. A permanent paragraph
    under them cost three lines, and a macOS popover that grows past the
    height the system allows puts the whole panel behind a scrollbar."""
    rows = systray.panel_rows(_state())
    assert [t for k, t in rows if k == "note"] == []


def test_the_tray_panel_shows_the_last_result_once_there_is_one():
    rows = systray.panel_rows(_state(connect_note="Claude is connected."))
    note = [t for k, t in rows if k == "note"][0]
    assert note == "Claude is connected."


def test_the_buttons_are_offered_even_while_the_vault_is_locked():
    """Wiring an agent edits the agent's config, not the vault, so a locked
    vault is no reason to hide it."""
    rows = systray.panel_rows(_state(locked=True))
    assert [t for k, t in rows if k.startswith("connect:")] == \
        ["Claude", "Hermes", "OpenClaw"]


@pytest.mark.parametrize("target,name", menubar.INTEGRATION_TARGETS)
def test_each_row_carries_the_target_the_action_needs(target, name):
    rows = systray.panel_rows(_state())
    assert (f"connect:{target}", name) in rows
