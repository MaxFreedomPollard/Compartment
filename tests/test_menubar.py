"""The menu bar app's data layer.

The window itself needs macOS, but everything it displays and every setting
it writes is plain Python - so it is tested on every OS. `import compartment.menubar`
must stay AppKit-free for that to hold.
"""
import json
import pathlib
import subprocess
import sys

import pytest

from compartment import menubar
from compartment.acl import VaultConfig

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
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    s = menubar.read_settings(vault_path)
    assert s == {"capture_hook": False, "search_starter_facts": True,
                 "expire_memories": True, "auto_lock_minutes": 30}


def test_set_setting_persists_to_the_config_file(vault_path, monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    menubar.set_setting(vault_path, "search_starter_facts", False)
    menubar.set_setting(vault_path, "auto_lock_minutes", 0)
    cfg = VaultConfig.load(vault_path)          # straight from disk
    assert cfg.settings["search_starter_facts"] is False
    assert cfg.settings["auto_lock_minutes"] == 0
    again = menubar.read_settings(vault_path)
    assert again["search_starter_facts"] is False
    assert again["auto_lock_minutes"] == 0


def test_settings_work_on_a_locked_vault(vault_path, monkeypatch):
    """The config file carries no secrets, so the popover stays usable when
    memory is locked - otherwise the toggles would need a passphrase."""
    from compartment import claude_hooks
    from compartment.vault import Vault
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    Vault.create(vault_path, PASS, creator="t").lock()
    menubar.set_setting(vault_path, "search_starter_facts", False)
    assert menubar.read_settings(vault_path)["search_starter_facts"] is False


def test_the_expiry_toggle_is_on_by_default_and_persists(vault_path,
                                                        monkeypatch):
    """The one setting this release adds. An expiry the user took the trouble
    to set should do something, so it starts on; turning it off makes the
    date a label and deletes nothing."""
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    assert menubar.read_settings(vault_path)["expire_memories"] is True
    menubar.set_setting(vault_path, "expire_memories", False)
    assert VaultConfig.load(vault_path).settings["expire_memories"] is False
    assert menubar.read_settings(vault_path)["expire_memories"] is False
    menubar.set_setting(vault_path, "expire_memories", True)
    assert menubar.read_settings(vault_path)["expire_memories"] is True


def test_unknown_setting_is_rejected(vault_path, monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    with pytest.raises(KeyError):
        menubar.set_setting(vault_path, "not_a_setting", 1)


def test_capture_hook_toggle_calls_the_hook_installer(vault_path, monkeypatch):
    from compartment import claude_hooks
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
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    st = menubar.fetch_state(str(tmp_path / "nope.vault"))
    assert st["exists"] is False and st["recent"] == []
    assert menubar.summarise(st) == "no vault"
    # It must point at the control that is on screen. The .pkg installs
    # Compartment.app and puts nothing on PATH, so "run: compartment init"
    # named a command the machine reading it does not have.
    assert "compartment init" not in st["error"]
    assert "passphrase" in st["error"]


# --------------------------------------------------------- creating a vault

def test_creating_a_vault_needs_a_passphrase(tmp_path):
    ok, msg = menubar.create_vault(str(tmp_path / "v.vault"), "", "")
    assert ok is False and "passphrase" in msg


def test_the_two_entries_have_to_match(tmp_path):
    ok, msg = menubar.create_vault(str(tmp_path / "v.vault"), "one", "two")
    assert ok is False and "do not match" in msg


def test_an_existing_vault_is_never_overwritten(vault_path):
    open(vault_path, "wb").write(b"x")
    ok, msg = menubar.create_vault(vault_path, PASS, PASS)
    assert ok is False and "already a vault" in msg


def test_the_passphrase_goes_down_stdin_and_never_into_argv(tmp_path,
                                                            monkeypatch):
    """A command line is readable by every process on the machine."""
    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "vault ready", ""

    def fake(args, **kw):
        seen["args"], seen["input"] = args, kw.get("input")
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake)
    ok, msg = menubar.create_vault(str(tmp_path / "v.vault"), PASS, PASS)
    assert ok is True and msg
    assert "--passphrase-stdin" in seen["args"]
    assert seen["input"].strip() == PASS
    assert PASS not in " ".join(seen["args"])
    assert "init" in seen["args"]


def test_creating_from_the_app_does_not_start_a_second_app(tmp_path,
                                                           monkeypatch):
    """`init` loads the login agent and starts the status bar app. The app
    asking for this IS the status bar app, and two copies then fight over
    one menu bar."""
    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: (seen.update(args=args), _Done())[1])
    menubar.create_vault(str(tmp_path / "v.vault"), PASS, PASS)
    assert "--no-app" in seen["args"]


def test_a_failed_creation_says_why(tmp_path, monkeypatch):
    class _Failed:
        returncode, stdout, stderr = 1, "", "disk is full"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    ok, msg = menubar.create_vault(str(tmp_path / "v.vault"), PASS, PASS)
    assert ok is False and "disk is full" in msg


def test_a_creation_that_cannot_run_the_cli_says_so(tmp_path, monkeypatch):
    """argparse's usage line is about our command line, never about the
    vault - the panel used to read a vault diagnosis out of one."""
    class _Usage:
        returncode, stdout, stderr = 2, "", "usage: compartment [-h] ..."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Usage())
    ok, msg = menubar.create_vault(str(tmp_path / "v.vault"), PASS, PASS)
    assert ok is False and "could not run the CLI" in msg


def test_stderr_noise_cannot_hide_the_vault_status(monkeypatch):
    """A CLI child that answers on stdout while a library warns on stderr is
    still an answer. onnxruntime's device discovery does exactly that on
    Azure/Hyper-V, on every call - and the panel read the mix as "could not
    read vault status" and showed an unlocked vault as locked."""
    monkeypatch.setattr(menubar, "_cli_argv", lambda: [
        sys.executable, "-c",
        "import sys;"
        "print('{\"locked\": false, \"records\": 1}');"
        "print('[W:onnxruntime:Default] pci noise', file=sys.stderr)"])
    assert menubar._json_cmd("ignored", "status") == {"locked": False,
                                                      "records": 1}


def test_creating_a_vault_really_works(tmp_path, monkeypatch):
    """End to end, through the real CLI: the panel's button is the only way
    in on an install that never put `compartment` on PATH, so this path is
    not allowed to be only unit-tested."""
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home" / ".hermes"))
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "home" / ".openclaw"))
    vault = str(tmp_path / "home" / ".compartment" / "memory.vault")
    ok, msg = menubar.create_vault(vault, PASS, PASS)
    assert ok is True, msg
    assert pathlib.Path(vault).exists()
    st = menubar.fetch_state(vault)
    assert st["exists"] is True and st["locked"] is False
    assert st["records"] > 0


def test_fetch_state_uses_the_cli(vault_path, monkeypatch):
    """State comes from subprocess calls, so the app never holds the model
    in memory. Stub them and assert the shape it builds."""
    from compartment import claude_hooks
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
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    open(vault_path, "wb").write(b"x")
    monkeypatch.setattr(menubar, "_json_cmd",
                        lambda v, *s: {"locked": True, "records": 5}
                        if s[0] == "status" else pytest.fail("must not run"))
    st = menubar.fetch_state(vault_path)
    assert st["locked"] is True and st["recent"] == []
    assert "unlock" in menubar.summarise(st)


def test_fetch_state_survives_a_broken_cli(vault_path, monkeypatch):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    open(vault_path, "wb").write(b"x")
    monkeypatch.setattr(menubar, "_json_cmd", lambda v, *s: None)
    st = menubar.fetch_state(vault_path)
    assert st["error"] == "could not read vault status"


def test_compartment_bin_never_returns_the_app_launcher(tmp_path, monkeypatch):
    """Regression: inside Compartment.app the interpreter sits beside a launcher
    named `Compartment`, and macOS filesystems are case-insensitive - so looking
    for "compartment" next to it found the launcher, and the app shelled out to
    ITSELF (relaunching the menu bar) instead of running the CLI."""
    macos = tmp_path / "Compartment.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "compartment").write_text("#!/bin/sh\n", encoding="utf-8")  # the trap
    (macos / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(macos / "python"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere"))
    monkeypatch.setattr(menubar.shutil, "which",
                        lambda n, path=None: "/usr/bin/compartment")
    assert menubar.compartment_bin() == "/usr/bin/compartment"


def test_compartment_bin_prefers_the_environment_console_script(tmp_path,
                                                           monkeypatch):
    binf = tmp_path / "bin"
    binf.mkdir()
    (binf / "compartment").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert menubar.compartment_bin() == str(binf / "compartment")


def test_state_runs_the_cli_through_our_own_interpreter():
    """No dependence on finding a console script on PATH, and it must be OUR
    interpreter - same prefix, so the CLI it imports is the one shipped
    beside this app rather than whatever Python happens to be around."""
    argv = menubar._cli_argv()
    assert argv[1:] == ["-m", "compartment.cli"]
    probe = subprocess.run([argv[0], "-c", "import sys; print(sys.prefix)"],
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == sys.prefix


def test_auto_lock_labels_cover_every_choice():
    labels = [menubar.auto_lock_label(m) for m in menubar.AUTO_LOCK_CHOICES]
    assert labels == ["15 min", "30 min", "60 min", "Never"]
    assert 0 in menubar.AUTO_LOCK_CHOICES        # "Never" must be selectable


def test_self_check_runs_without_a_window(vault_path, monkeypatch, capsys):
    from compartment import claude_hooks
    monkeypatch.setattr(claude_hooks, "is_installed", lambda *a, **k: False)
    assert menubar.self_check(vault_path) == 0
    assert "vault" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS can run the app")
def test_run_refuses_politely_off_macos(capsys):
    assert menubar.run() == 1
    assert "macOS only" in capsys.readouterr().err


# --------------------------------------------------- first launch, login item

def test_first_run_is_claimed_exactly_once(tmp_path):
    """The first launch opens the panel by itself. The second must not."""
    vault = tmp_path / "memory.vault"
    assert menubar.claim_first_run(str(vault)) is True
    assert menubar.claim_first_run(str(vault)) is False
    assert (tmp_path / menubar.FIRST_RUN_MARKER).exists()


def test_first_run_survives_an_unwritable_home(tmp_path):
    """A read-only home costs the intro, never the app."""
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory")
    assert menubar.claim_first_run(str(blocked / "sub" / "memory.vault")) is False


class _Svc:
    """Stand-in for SMAppService.mainAppService()."""

    def __init__(self, result, status=1):
        self._result, self._status = result, status

    def registerAndReturnError_(self, _):
        return self._result

    def unregisterAndReturnError_(self, _):
        return self._result

    def status(self):
        return self._status


class _Err:
    @staticmethod
    def localizedDescription():
        return "Could not connect to system service"


def test_set_login_reports_a_refusal_instead_of_claiming_success(monkeypatch):
    """The NSError out-parameter used to be dropped, so registering from a
    root installer script - which macOS refuses - looked like it worked."""
    monkeypatch.setattr(menubar, "_app_service", lambda: _Svc((False, _Err())))
    out = menubar.set_login(True)
    assert out.startswith("failed:")
    assert "Could not connect to system service" in out


def test_set_login_reports_success(monkeypatch):
    monkeypatch.setattr(menubar, "_app_service", lambda: _Svc((True, None)))
    assert menubar.set_login(True) == "enabled"


def test_set_login_tolerates_a_bare_bool(monkeypatch):
    """Older PyObjC hands back just the BOOL rather than a tuple."""
    monkeypatch.setattr(menubar, "_app_service", lambda: _Svc(True))
    assert menubar.set_login(True) == "enabled"


# ------------------------------------------------------- unlocking from the UI

def test_unlock_never_puts_the_passphrase_in_argv(monkeypatch):
    """A command line is readable by every process on the machine. The panel
    writes the secret down the child's stdin instead."""
    seen = {}

    class Done:
        returncode = 0
        stdout = "unlocked: stays unlocked"
        stderr = ""

    def fake_run(args, **kw):
        seen["argv"] = args
        seen["input"] = kw.get("input")
        return Done()

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)
    ok, note = menubar.unlock_vault("/v", "correct horse")
    assert ok and note == "unlocked"
    assert "correct horse" not in " ".join(seen["argv"])
    assert "--passphrase-stdin" in seen["argv"]
    assert seen["input"] == "correct horse\n"


@pytest.mark.parametrize("out,expected", [
    ("error: Wrong passphrase (no keyslot opened)", "wrong passphrase"),
    ("error: keyfile not found at /Volumes/stick/key", 
     "needs its 2FA keyfile - plug it in and try again"),
])
def test_unlock_failures_are_explained_not_dumped(monkeypatch, out, expected):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = out

    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: Failed())
    ok, note = menubar.unlock_vault("/v", "nope")
    assert not ok and note == expected


def test_unlock_with_no_passphrase_never_runs_anything(monkeypatch):
    monkeypatch.setattr(menubar.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not shell out"))
    ok, note = menubar.unlock_vault("/v", "")
    assert not ok and "passphrase" in note


# -------------------------------------------------- changing the passphrase

def test_change_passphrase_never_puts_the_secret_in_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")

        class Done:
            returncode = 0
            stdout = "credential replaced."
            stderr = ""
        return Done()

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)
    ok, note = menubar.change_passphrase("/v", "Locksmith", "Locksmith")
    assert ok and note == "passphrase changed"
    assert "Locksmith" not in " ".join(seen["argv"]), seen["argv"]
    assert "--new-passphrase-stdin" in seen["argv"]
    assert seen["input"] == "Locksmith\n"


@pytest.mark.parametrize("new,repeat,expected", [
    ("", "", "enter a new passphrase"),
    ("Locksmith", "Locksmyth", "the two entries do not match"),
])
def test_change_passphrase_validates_before_shelling_out(monkeypatch, new,
                                                         repeat, expected):
    monkeypatch.setattr(menubar.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not shell out"))
    ok, note = menubar.change_passphrase("/v", new, repeat)
    assert not ok and note == expected


def test_change_passphrase_on_a_locked_vault_says_what_to_do(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "error: vault is locked"

    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: Failed())
    ok, note = menubar.change_passphrase("/v", "Locksmith", "Locksmith")
    assert not ok and note == "unlock the vault first"


def test_change_passphrase_does_not_blame_the_vault_for_a_cli_failure(
        monkeypatch):
    class Failed:
        returncode = 2
        stdout = ""
        stderr = "usage: compartment [-h] [--keyfile KEYFILE]\ncompartment: error: unrecognized arguments: rekey"

    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: Failed())
    ok, note = menubar.change_passphrase("/v", "Locksmith", "Locksmith")
    assert not ok and "keyfile" not in note.lower(), note


def test_a_broken_cli_call_is_never_blamed_on_the_vault(monkeypatch):
    """argparse prints "[--keyfile KEYFILE]" in its usage line. That once
    reached the user as "needs its 2FA keyfile" on a vault that had no 2FA,
    while the real fault was the app invoking its own CLI wrongly."""
    class Failed:
        returncode = 2
        stdout = ""
        stderr = ("usage: compartment [-h] [--vault VAULT] [--keyfile KEYFILE]\n"
                  "compartment: error: unrecognized arguments: --vault /v unlock")

    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: Failed())
    ok, note = menubar.unlock_vault("/v", "whatever")
    assert not ok
    assert "keyfile" not in note.lower(), note
    assert "2fa" not in note.lower(), note


def test_cli_argv_points_at_a_real_interpreter_not_the_app_launcher():
    """Inside the .app, sys.executable is the bundle launcher, which always
    runs `compartment.cli menubar` - so using it made every CLI call the
    panel issued fail in argparse."""
    argv = menubar._cli_argv()
    assert argv[1:] == ["-m", "compartment.cli"]
    exe = pathlib.Path(argv[0])
    assert exe.exists(), exe
    # The launcher lives in Contents/MacOS and is named for the app; a real
    # interpreter never is.
    assert exe.parent.name != "MacOS", f"{exe} is the bundle launcher"
    probe = subprocess.run([str(exe), "-c", "import sys; print(sys.executable)"],
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr


def test_dashboard_url_parses_only_the_announcement_line():
    from compartment.menubar import dashboard_url
    assert dashboard_url("Compartment dashboard: http://127.0.0.1:5/t/\n") == \
        "http://127.0.0.1:5/t/"
    assert dashboard_url("  serving from RAM, 127.0.0.1 only") is None


def test_the_dashboard_button_starts_dash_once_and_reuses_it(monkeypatch,
                                                              tmp_path):
    """First click: start `compartment dash`, which opens the browser itself.
    Second click: reopen the URL it announced, never a second server."""
    import sys
    from compartment import menubar
    script = ("import time\n"
              "print('Compartment dashboard: http://127.0.0.1:1/abc/', flush=True)\n"
              "print('  serving from RAM', flush=True)\n"
              "time.sleep(30)\n")
    monkeypatch.setattr(menubar, "_cli_argv",
                        lambda: [sys.executable, "-c", script])
    opened = []
    monkeypatch.setattr(menubar.webbrowser, "open",
                        lambda u: opened.append(u) or True)
    menubar._DASH.update(proc=None, url=None)
    vault = str(tmp_path / "x.vault")
    url = menubar.open_dashboard(vault)
    assert url == "http://127.0.0.1:1/abc/"
    proc = menubar._DASH["proc"]
    assert proc is not None and proc.poll() is None
    assert opened == []                      # dash opened the browser itself
    assert menubar.open_dashboard(vault) == url
    assert menubar._DASH["proc"] is proc and opened == [url]
    menubar.stop_dashboard()
    assert proc.poll() is not None and menubar._DASH["proc"] is None


def test_a_dash_that_does_not_start_gives_no_url(monkeypatch, tmp_path):
    import sys
    from compartment import menubar
    monkeypatch.setattr(menubar, "_cli_argv", lambda: [
        sys.executable, "-c", "import sys; print('error: locked'); sys.exit(2)"])
    menubar._DASH.update(proc=None, url=None)
    assert menubar.open_dashboard(str(tmp_path / "x.vault")) is None
    assert menubar._DASH["proc"] is None
