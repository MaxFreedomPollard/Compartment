"""The Windows tray app, tested from every OS.

The widgets are the only Windows-specific part; everything the panel *says*
comes from the shared data layer, so these run on the Linux and macOS CI
legs too and would catch a Windows-only break before the Windows leg ever
starts a GUI.
"""
from __future__ import annotations

import sys

import pytest

from compartment import systray


def _state(**over) -> dict:
    base = {"vault": "/v", "exists": True, "locked": False, "records": 6723,
            "organic": 5, "recent": [{"text": "a fact"}, {"text": "another"}],
            "error": None,
            "settings": {"capture_hook": True, "search_starter_facts": False,
                         "auto_lock_minutes": 30}}
    base.update(over)
    return base


def _kinds(rows):
    return [k for k, _ in rows]


def test_panel_shows_state_settings_and_memories():
    rows = systray.panel_rows(_state())
    assert rows[0][0] == "state"
    assert "6,723 memories" in rows[0][1]
    kinds = _kinds(rows)
    assert "toggle:capture_hook" in kinds
    assert "toggle:search_starter_facts" in kinds
    assert "choice:auto_lock_minutes" in kinds
    assert kinds.count("memory") == 2


def test_panel_reports_every_setting_state():
    rows = dict(systray.panel_rows(_state()))
    assert rows["toggle:capture_hook"].endswith("on")
    assert rows["toggle:search_starter_facts"].endswith("off")
    assert rows["choice:auto_lock_minutes"].endswith("30 min")
    never = dict(systray.panel_rows(
        _state(settings={"capture_hook": False, "search_starter_facts": True,
                         "auto_lock_minutes": 0})))
    assert never["choice:auto_lock_minutes"].endswith("Never")


def test_locked_vault_says_so_and_lists_nothing():
    rows = systray.panel_rows(_state(locked=True, recent=[]))
    assert "locked" in rows[0][1]
    assert "empty" in _kinds(rows)
    assert "memory" not in _kinds(rows)


def test_the_panel_offers_unlock_when_locked_and_lock_when_open():
    """Opening the vault is the thing people do most, and it should not send
    them to a terminal to do it."""
    assert "unlock" in _kinds(systray.panel_rows(_state(locked=True, recent=[])))
    assert "lock" not in _kinds(systray.panel_rows(_state(locked=True, recent=[])))
    assert "lock" in _kinds(systray.panel_rows(_state()))
    assert "unlock" not in _kinds(systray.panel_rows(_state()))


def test_change_password_is_offered_only_on_an_open_vault():
    """rekey re-wraps the master key, which the process only holds while the
    vault is unlocked - so the button must not appear when it is locked."""
    assert "change" in _kinds(systray.panel_rows(_state()))
    assert "change" not in _kinds(systray.panel_rows(_state(locked=True,
                                                           recent=[])))
    assert "change" not in _kinds(systray.panel_rows(_state(exists=False,
                                                           recent=[])))


def test_no_vault_offers_neither():
    kinds = _kinds(systray.panel_rows(_state(exists=False, recent=[])))
    assert "unlock" not in kinds and "lock" not in kinds


def test_missing_vault_and_errors_are_shown_not_raised():
    rows = systray.panel_rows(_state(exists=False, recent=[],
                                     error="no vault yet - run: compartment init"))
    assert rows[0][1] == "no vault"
    assert ("error", "no vault yet - run: compartment init") in rows


def test_settings_toggles_are_exactly_the_three_the_mac_panel_has():
    from compartment import menubar
    rows = systray.panel_rows(_state())
    keys = {k.split(":", 1)[1] for k, _ in rows if ":" in k}
    assert keys == set(menubar.fetch_state("/does/not/exist")["settings"])


def test_tray_icon_ships_with_the_package():
    assert systray.icon_path().is_file(), "tools/make_icon.py must be run"
    Image = pytest.importorskip("PIL.Image")
    with Image.open(systray.icon_path()) as ico:
        # Windows picks a size per DPI and per surface; one 256px frame alone
        # gets downscaled to mush in the notification area.
        assert len(ico.ico.sizes()) >= 4
        assert (16, 16) in ico.ico.sizes()


def test_cli_picks_the_front_end_for_the_platform(monkeypatch):
    from compartment import cli, menubar
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli._tray_app() is systray
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli._tray_app() is menubar
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli._tray_app() is menubar


@pytest.mark.skipif(sys.platform != "win32", reason="the Run key is Windows")
def test_login_toggle_round_trips():
    before = systray.login_status()
    try:
        assert systray.set_login(True) == "on"
        assert systray.login_status() == "on"
        assert systray.set_login(False) == "off"
        assert systray.login_status() == "off"
    finally:
        systray.set_login(before == "on")


@pytest.mark.skipif(sys.platform == "win32", reason="asks for the other OS")
def test_login_helpers_report_instead_of_raising_off_windows():
    # No winreg here, so both must degrade to a message, not an exception.
    assert isinstance(systray.login_status(), str)
    assert systray.set_login(True).startswith("error:")
