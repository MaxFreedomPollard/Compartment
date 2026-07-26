"""The menu bar app's data layer.

The window itself needs macOS, but everything it displays and every setting
it writes is plain Python - so it is tested on every OS. `import engram.menubar`
must stay AppKit-free for that to hold.
"""
import json
import sys

import pytest

from engram import menubar
from engram.acl import VaultConfig

PASS = "CorrectHorse"


def test_module_imports_without_appkit():
    """Guard the split: AppKit lives inside run(), never at import time."""
    assert "AppKit" not in sys.modules or sys.platform == "darwin"
    src = (menubar.__file__ or "")
    assert src.endswith("menubar.py")
    text = open(src, encoding="utf-8").read()
    head = text.split("def run(")[0]
    assert "import AppKit" not in head and "from AppKit" not in head


# ------------------------------------------------------------- settings

def test_read_settings_defaults_for_a_fresh_vault(vault_path, monkeypatch):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    s = menubar.read_settings(vault_path)
    assert s == {"capture_hook": False, "search_starter_facts": True,
                 "auto_lock_minutes": 30}


def test_set_setting_persists_to_the_config_file(vault_path, monkeypatch):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    menubar.set_setting(vault_path, "search_starter_facts", False)
    menubar.set_setting(vault_path, "auto_lock_minutes", 0)
    cfg = VaultConfig.load(vault_path)          # straight from disk
    assert cfg.settings["include_packs_in_search"] is False
    assert cfg.settings["auto_lock_minutes"] == 0
    again = menubar.read_settings(vault_path)
    assert again["search_starter_facts"] is False
    assert again["auto_lock_minutes"] == 0


def test_settings_work_on_a_locked_vault(vault_path, monkeypatch):
    """The config file carries no secrets, so the popover stays usable when
    memory is locked - otherwise the toggles would need a passphrase."""
    from engram import claude_hooks
    from engram.vault import Vault
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    Vault.create(vault_path, PASS, creator="t").lock()
    menubar.set_setting(vault_path, "search_starter_facts", False)
    assert menubar.read_settings(vault_path)["search_starter_facts"] is False


def test_unknown_setting_is_rejected(vault_path, monkeypatch):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    with pytest.raises(KeyError):
        menubar.set_setting(vault_path, "not_a_setting", 1)


def test_capture_hook_toggle_calls_the_hook_installer(vault_path, monkeypatch):
    from engram import claude_hooks
    calls = []
    monkeypatch.setattr(claude_hooks, "install",
                        lambda **kw: calls.append(("install", kw)))
    monkeypatch.setattr(claude_hooks, "uninstall", lambda: calls.append(("off",)))
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: True)
    menubar.set_setting(vault_path, "capture_hook", True)
    menubar.set_setting(vault_path, "capture_hook", False)
    assert [c[0] for c in calls] == ["install", "off"]
    assert calls[0][1]["vault"] == vault_path


# ---------------------------------------------------------------- state

def test_fetch_state_reports_a_missing_vault_instead_of_raising(tmp_path,
                                                                monkeypatch):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    st = menubar.fetch_state(str(tmp_path / "nope.vault"))
    assert st["exists"] is False and st["recent"] == []
    assert "engram init" in st["error"]
    assert menubar.summarise(st) == "no vault"


def test_fetch_state_uses_the_cli(vault_path, monkeypatch):
    """State comes from subprocess calls, so the app never holds the model
    in memory. Stub them and assert the shape it builds."""
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: True)
    open(vault_path, "wb").write(b"x")          # just needs to exist

    def fake(vault, *sub):
        if sub[0] == "status":
            return {"locked": False, "records": 100, "organic_records": 7}
        return {"counts": {"total": 100, "organic": 7},
                "results": [{"text": "older", "created_local": "2026-01-01 00:00",
                             "tags": ["a"]},
                            {"text": "newest", "created_local": "2026-01-02 00:00",
                             "tags": ["b"]}]}

    monkeypatch.setattr(menubar, "_json_cmd", fake)
    st = menubar.fetch_state(vault_path)
    assert st["locked"] is False and st["records"] == 100 and st["organic"] == 7
    # newest first: the list is glanced at, not scrolled
    assert [r["text"] for r in st["recent"]] == ["newest", "older"]
    assert menubar.summarise(st) == "100 memories · 7 stored by you"


def test_fetch_state_skips_recent_when_locked(vault_path, monkeypatch):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    open(vault_path, "wb").write(b"x")
    monkeypatch.setattr(menubar, "_json_cmd",
                        lambda v, *s: {"locked": True, "records": 5}
                        if s[0] == "status" else pytest.fail("must not run"))
    st = menubar.fetch_state(vault_path)
    assert st["locked"] is True and st["recent"] == []
    assert "unlock" in menubar.summarise(st)


def test_fetch_state_survives_a_broken_cli(vault_path, monkeypatch):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    open(vault_path, "wb").write(b"x")
    monkeypatch.setattr(menubar, "_json_cmd", lambda v, *s: None)
    st = menubar.fetch_state(vault_path)
    assert st["error"] == "could not read vault status"


def test_engram_bin_never_returns_the_app_launcher(tmp_path, monkeypatch):
    """Regression: inside engRAM.app the interpreter sits beside a launcher
    named `engRAM`, and macOS filesystems are case-insensitive - so looking
    for "engram" next to it found the launcher, and the app shelled out to
    ITSELF (relaunching the menu bar) instead of running the CLI."""
    macos = tmp_path / "engRAM.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "engram").write_text("#!/bin/sh\n", encoding="utf-8")  # the trap
    (macos / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(macos / "python"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere"))
    monkeypatch.setattr(menubar.shutil, "which", lambda n: "/usr/bin/engram")
    assert menubar.engram_bin() == "/usr/bin/engram"


def test_engram_bin_prefers_the_environment_console_script(tmp_path,
                                                           monkeypatch):
    binf = tmp_path / "bin"
    binf.mkdir()
    (binf / "engram").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert menubar.engram_bin() == str(binf / "engram")


def test_state_runs_the_cli_through_our_own_interpreter():
    """No dependence on finding a console script on PATH."""
    argv = menubar._cli_argv()
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "engram.cli"]


def test_auto_lock_labels_cover_every_choice():
    labels = [menubar.auto_lock_label(m) for m in menubar.AUTO_LOCK_CHOICES]
    assert labels == ["15 min", "30 min", "60 min", "Never"]
    assert 0 in menubar.AUTO_LOCK_CHOICES        # "Never" must be selectable


def test_self_check_runs_without_a_window(vault_path, monkeypatch, capsys):
    from engram import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    assert menubar.self_check(vault_path) == 0
    assert "vault" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS can run the app")
def test_run_refuses_politely_off_macos(capsys):
    assert menubar.run() == 1
    assert "macOS only" in capsys.readouterr().err
