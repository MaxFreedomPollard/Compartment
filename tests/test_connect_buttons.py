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

    Containment is one-way now. The panel offers the five one-click agents,
    while the CLI accepts every MCP client as well. Thirty buttons would not
    fit in a popover, so the panel is a subset on purpose.
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
        ["claude", "hermes", "openclaw", "antigravity", "codex"]


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


def _connects(monkeypatch, target):
    """A machine where `target` is installed and the wiring lands."""
    seen = {"n": 0}

    def status(_vault):
        seen["n"] += 1
        return {target: seen["n"] > 1}      # not wired before, wired after
    monkeypatch.setattr(menubar, "agent_present", lambda t: True)
    monkeypatch.setattr(menubar, "integration_status", status)


def test_it_runs_the_documented_command(monkeypatch):
    seen = {}

    def fake_run(args, timeout=60):
        seen["args"] = args
        return 0, "registered."
    _connects(monkeypatch, "claude")
    monkeypatch.setattr(menubar, "_run", fake_run)
    ok, _ = menubar.integrate("/path/to/v.vault", "claude")
    assert ok is True
    assert seen["args"][-3:] == ["v.vault", "integrate", "claude"] or \
        seen["args"][-2:] == ["integrate", "claude"]
    assert "--vault" in seen["args"]
    assert "/path/to/v.vault" in seen["args"]


def test_success_tells_the_user_to_restart_the_agent(monkeypatch):
    _connects(monkeypatch, "claude")
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: (0, "registered."))
    ok, msg = menubar.integrate("/v", "claude")
    assert ok is True
    assert "Claude" in msg and "estart" in msg


def test_a_missing_agent_is_reported_as_normal_not_as_failure(monkeypatch):
    """Not having Hermes installed is not an error, and must not read as
    one: the wiring that can be done is still done."""
    monkeypatch.setattr(menubar, "agent_present", lambda t: False)
    monkeypatch.setattr(menubar, "integration_status", lambda v: {})
    monkeypatch.setattr(menubar, "_run",
                        lambda *a, **k: (0, "hermes not found; finish with…"))
    ok, msg = menubar.integrate("/v", "hermes")
    assert ok is True
    assert "Could not find Hermes" in msg


def test_the_verdict_is_never_read_out_of_the_commands_prose(monkeypatch):
    """"not found" appears in the output of a run that connected an agent
    perfectly, because `integrate claude` wires two programs and reports on
    both. What the panel says comes from the agent's own configuration."""
    _connects(monkeypatch, "claude")
    monkeypatch.setattr(menubar, "_run", lambda *a, **k: (
        0, "Claude Code CLI not found; register manually with: ..."))
    ok, msg = menubar.integrate("/v", "claude")
    assert ok is True and "Could not find" not in msg


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

def test_the_tray_panel_offers_the_first_run_step(monkeypatch):
    """With no vault there is nothing to unlock, so the Unlock control is
    correctly hidden - which used to leave the panel with no control at all
    and a pointer to a terminal command the app install never provided."""
    kinds = [k for k, _ in systray.panel_rows(_state(exists=False, locked=True,
                                                     recent=[]))]
    assert "create" in kinds
    assert "unlock" not in kinds and "lock" not in kinds


def test_the_tray_panel_has_a_button_per_agent():
    rows = systray.panel_rows(_state())
    connects = [text for kind, text in rows if kind.startswith("connect:")]
    assert connects == ["Claude", "Hermes", "OpenClaw", "AntiGravity", "Codex"]


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
        ["Claude", "Hermes", "OpenClaw", "AntiGravity", "Codex"]


@pytest.mark.parametrize("target,name", menubar.INTEGRATION_TARGETS)
def test_each_row_carries_the_target_the_action_needs(target, name):
    rows = systray.panel_rows(_state())
    assert (f"connect:{target}", name) in rows


def test_all_connect_buttons_fit_on_one_line_on_macos():
    """All five Connect buttons must fit on one horizontal line inside
    CONTENT_WIDTH without compression, both plain and with checkmarks."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        pytest.skip("macOS AppKit layout check")
    pytest.importorskip("AppKit")
    code = (
        "import AppKit\n"
        "from compartment import menubar\n"
        "AppKit.NSApplication.sharedApplication()\n"
        "for checked in (False, True):\n"
        "    buttons = []\n"
        "    for _t, name in menubar.INTEGRATION_TARGETS:\n"
        "        b = AppKit.NSButton.buttonWithTitle_target_action_(\n"
        "            f'{name} \\u2713' if checked else name, None, None)\n"
        "        b.setControlSize_(1)\n"
        "        buttons.append(b)\n"
        "    spacer = AppKit.NSView.alloc().init()\n"
        "    spacer.setContentHuggingPriority_forOrientation_(1, 0)\n"
        "    row = AppKit.NSStackView.stackViewWithViews_([*buttons, spacer])\n"
        "    row.setOrientation_(0)\n"
        "    row.setSpacing_(5)\n"
        "    row.setAlignment_(10)\n"
        "    assert row.fittingSize().width <= menubar.CONTENT_WIDTH\n"
        "    row.widthAnchor().constraintEqualToConstant_(\n"
        "        menubar.CONTENT_WIDTH).setActive_(True)\n"
        "    row.layoutSubtreeIfNeeded()\n"
        "    assert len({round(b.frame().origin.y, 1) for b in buttons}) == 1\n"
        "    for b in buttons:\n"
        "        assert round(b.frame().size.width, 1) == round(\n"
        "            b.fittingSize().width, 1)\n"
        "    last = buttons[-1].frame()\n"
        "    assert last.origin.x + last.size.width <= menubar.CONTENT_WIDTH\n"
    )
    ran = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr


def test_all_connect_buttons_fit_on_one_line_in_tk():
    """The tray/panel row fits all five buttons inside PANEL_WIDTH."""
    import subprocess
    import sys
    pytest.importorskip("tkinter")
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import font as tkfont, ttk\n"
        "from compartment import menubar, systray\n"
        "try:\n"
        "    root = tk.Tk()\n"
        "except tk.TclError:\n"
        "    sys.exit(77)\n"
        "try:\n"
        "    root.withdraw()\n"
        "    style = ttk.Style()\n"
        "    usable = systray.PANEL_WIDTH - 28\n"
        "    themes = list(dict.fromkeys([\n"
        "        *([style.theme_use()] if style.theme_use() != 'aqua' else []),\n"
        "        *(t for t in ('clam', 'alt', 'default') if t in style.theme_names()),\n"
        "    ]))\n"
        "    for checked in (False, True):\n"
        "        for theme in themes:\n"
        "            style.theme_use(theme)\n"
        "            frame = ttk.Frame(root, padding=14)\n"
        "            row = ttk.Frame(frame)\n"
        "            row.pack(fill='x', pady=(6, 0))\n"
        "            df = tkfont.nametofont('TkDefaultFont').actual()\n"
        "            cfont = (('Segoe UI', 8) if sys.platform == 'win32'\n"
        "                     else (df['family'], max(9, abs(int(df['size'])) - 1)))\n"
        "            style.configure('Connect.TButton', padding=(3, 2), font=cfont)\n"
        "            for i, (_t, name) in enumerate(menubar.INTEGRATION_TARGETS):\n"
        "                ttk.Button(\n"
        "                    row, text=f'{name} \\u2713' if checked else name,\n"
        "                    width=0, style='Connect.TButton'\n"
        "                ).pack(side='left',\n"
        "                       padx=(0, 3 if i < len(menubar.INTEGRATION_TARGETS) - 1\n"
        "                             else 0))\n"
        "            frame.update_idletasks()\n"
        "            assert row.winfo_reqwidth() <= usable, (theme, row.winfo_reqwidth())\n"
        "            assert frame.winfo_reqwidth() <= systray.PANEL_WIDTH\n"
        "            frame.destroy()\n"
        "finally:\n"
        "    root.destroy()\n"
    )
    ran = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=60)
    if ran.returncode == 77:
        pytest.skip("no display for Tk layout check")
    assert ran.returncode == 0, ran.stderr
