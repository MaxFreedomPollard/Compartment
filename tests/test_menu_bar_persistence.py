"""The icon is there at every login, and goes only when somebody quits it.

test_stays_running.py covers the write path: the plist launchd is asked to
load, and the refusals that used to be swallowed on the way in. This file
covers everything that reported success without checking, and the question of
which copy of the app owns the menu bar.

  - `login_status` answered "on" for any plist file on disk. It never asked
    launchd, so a plist written by a bootstrap that failed reported start at
    login as working, at every login, for ever. `_bootstrap_agent` was
    hardened against exactly this and its own docstring describes it; the
    fix was applied to the write path and never to the status path, and the
    status path is the one a pip install reads (there is no app bundle, so
    `_app_service()` is None and the plist branch is the only branch).
  - `launchctl load` returns zero for jobs it declined, so the fallback verb
    could still report an agent that was never loaded.
  - Turning start at login off deleted the plist whether or not launchd let
    go of the job. With KeepAlive registered, that leaves launchd free to
    bring the icon back from a file that no longer exists.
  - `quit_running` reported that it had stopped the app when all it had done
    was send a signal.
  - `init` said "look for the icon in your menu bar" after spawning a process
    it never looked at again.
  - A `compartment menubar` started from a terminal dies with the terminal,
    and while it held the single-instance lock the copy launchd started stood
    down with exit 0 - which KeepAlive SuccessfulExit=false reads as a
    deliberate exit, so launchd never started it again either.
  - On Linux, switching the app off in GNOME Tweaks rewrites the autostart
    entry with Hidden=true rather than deleting it, and reading the filename
    alone called that "on".
  - On Windows the Run entry was read back by name but not by value.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from compartment import cli, menubar, systray


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(menubar.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _write_plist(home):
    path = (home / "Library" / "LaunchAgents"
            / f"{menubar.LAUNCH_AGENT_LABEL}.plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<plist/>\n", encoding="utf-8")
    return path


def _launchctl(**answers):
    """A launchctl that answers each verb the way the test wants, and records
    what it was asked. Default is success, because the interesting cases are
    the ones where a verb succeeds and the job still is not there."""
    calls = []

    def fake(*argv):
        calls.append(argv)
        return answers.get(argv[0], (0, ""))
    fake.calls = calls
    return fake


# --- status is what launchd says, not what the filesystem suggests ----------

def test_a_plist_launchd_never_loaded_is_not_reported_as_on(fake_home,
                                                            monkeypatch):
    """The bug, exactly as it stands on a real machine: the file is there,
    `launchctl print` exits 113, and the app says start at login is on."""
    _write_plist(fake_home)
    monkeypatch.setattr(menubar, "_app_service", lambda: None)
    monkeypatch.setattr(menubar, "_launchctl",
                        _launchctl(print=(113, "Could not find service")))
    assert menubar.login_status() != "on"


def test_status_says_on_when_launchd_really_has_the_agent(fake_home,
                                                          monkeypatch):
    _write_plist(fake_home)
    monkeypatch.setattr(menubar, "_app_service", lambda: None)
    monkeypatch.setattr(menubar, "_launchctl", _launchctl())
    assert menubar.login_status() == "on"


def test_status_asks_launchd_and_not_the_filesystem(fake_home, monkeypatch):
    """Not just the right answer - the right question. Nothing else can tell
    a registration apart from a file that was written and refused."""
    _write_plist(fake_home)
    fake = _launchctl()
    monkeypatch.setattr(menubar, "_app_service", lambda: None)
    monkeypatch.setattr(menubar, "_launchctl", fake)
    menubar.login_status()
    assert any(c[0] == "print" for c in fake.calls), fake.calls
    assert any(menubar.LAUNCH_AGENT_LABEL in c[-1] for c in fake.calls)


def test_no_plist_is_still_plainly_off(fake_home, monkeypatch):
    monkeypatch.setattr(menubar, "_app_service", lambda: None)
    monkeypatch.setattr(menubar, "_launchctl", _launchctl())
    assert menubar.login_status() == "off"


def test_the_bundle_install_still_asks_the_service(fake_home, monkeypatch):
    """A .pkg install has SMAppService, which is its own authority. That path
    must not start consulting a plist it does not use."""
    monkeypatch.setattr(menubar, "_app_service",
                        lambda: type("S", (), {"status": lambda self: 1})())
    assert menubar.login_status() == "enabled"


# --- the write path does not believe launchctl either -----------------------

def test_load_returning_zero_is_not_proof_the_agent_loaded(fake_home,
                                                            monkeypatch):
    """`launchctl load` exits zero for jobs it declined. bootstrap fails,
    load "succeeds", and launchd still does not have the job."""
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/bin/true"])
    monkeypatch.setattr(menubar, "_launchctl", _launchctl(
        bootstrap=(1, "Bootstrap failed: 5: Input/output error"),
        load=(0, ""),
        print=(113, "Could not find service")))
    assert menubar._set_login_agent(True).startswith("failed")


def test_turning_it_off_checks_that_launchd_let_go(fake_home, monkeypatch):
    """Deleting the plist is not what stops it. launchd holds the job in
    memory once bootstrapped, so a bootout that failed leaves KeepAlive free
    to put the icon back after the user asked for it to be gone."""
    _write_plist(fake_home)
    monkeypatch.setattr(menubar, "_launchctl", _launchctl(
        bootout=(125, "Boot-out failed: 36: Operation now in progress"),
        print=(0, "")))                       # ...and it is still registered
    assert menubar._set_login_agent(False).startswith("failed")


def test_turning_it_off_is_fine_when_nothing_was_loaded(fake_home,
                                                         monkeypatch):
    """3 is launchd's "no such process", which is the state we wanted."""
    _write_plist(fake_home)
    monkeypatch.setattr(menubar, "_launchctl",
                        _launchctl(bootout=(3, "No such process")))
    assert menubar._set_login_agent(False) == "off"


# --- stopping the app means it stopped --------------------------------------

def test_quit_running_waits_for_the_process_to_actually_go(monkeypatch):
    """It used to report success for a signal it had sent. The caller's next
    move is to start the new build, and that must not happen while the old
    copy still holds the lock."""
    monkeypatch.setattr(menubar, "running_pids", lambda: [4242])
    monkeypatch.setattr(menubar.os, "kill", lambda *a: None)
    assert menubar.quit_running(timeout=0.3) is False


def test_quit_running_reports_success_once_it_is_gone(monkeypatch):
    seen = []
    monkeypatch.setattr(menubar, "running_pids",
                        lambda: [] if seen else (seen.append(1), [4242])[1])
    monkeypatch.setattr(menubar.os, "kill", lambda *a: None)
    assert menubar.quit_running(timeout=5) is True


def test_running_pids_never_counts_this_process(monkeypatch):
    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": f"{os.getpid()}\n77\n"})())
    assert menubar.running_pids() == [77]


# --- which copy owns the menu bar -------------------------------------------

def test_only_the_launchd_copy_counts_as_supervised(monkeypatch):
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert menubar._is_launchd_managed() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")       # what a shell gets
    assert menubar._is_launchd_managed() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", menubar.LAUNCH_AGENT_LABEL)
    assert menubar._is_launchd_managed() is True


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows mandatory-locks the byte, so nobody else "
                           "could read the pid back out of it")
def test_the_lock_names_the_process_holding_it(tmp_path):
    """So the copy entitled to the menu bar asks one process for it, rather
    than killing every icon on a machine that runs two vaults."""
    vault = str(tmp_path / "memory.vault")
    menubar.acquire_instance_lock(vault)
    body = (tmp_path / menubar.INSTANCE_LOCK_NAME).read_text(encoding="utf-8")
    assert body.strip() == str(os.getpid())


@pytest.mark.skipif(sys.platform == "win32",
                    reason="no pid is recorded on Windows; see "
                           "_record_lock_holder")
def test_the_holder_pid_is_read_back(tmp_path, monkeypatch):
    vault = str(tmp_path / "memory.vault")
    mine = os.getpid()
    menubar.acquire_instance_lock(vault)
    # From our own process the holder is us, which is nobody to evict.
    assert menubar.lock_holder_pid(vault) is None
    monkeypatch.setattr(menubar.os, "getpid", lambda: 999999)
    assert menubar.lock_holder_pid(vault) == mine


def test_eviction_touches_only_the_process_that_holds_this_vault(tmp_path,
                                                                 monkeypatch):
    vault = str(tmp_path / "memory.vault")
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / menubar.INSTANCE_LOCK_NAME).write_text("4242\n",
                                                       encoding="utf-8")
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == 0 and len(killed) > 1:
            raise ProcessLookupError(pid)         # it went
    monkeypatch.setattr(menubar.os, "kill", fake_kill)
    assert menubar.evict_unsupervised(vault, timeout=5) is True
    assert {pid for pid, _sig in killed} == {4242}


def test_an_incumbent_that_never_said_who_it_is_is_left_alone(tmp_path,
                                                              monkeypatch):
    """An empty lock file is every copy started before the pid was recorded.
    Standing down costs an icon until the next login; killing a process
    picked by guesswork costs more."""
    vault = str(tmp_path / "memory.vault")
    (tmp_path / menubar.INSTANCE_LOCK_NAME).write_text("", encoding="utf-8")
    monkeypatch.setattr(menubar.os, "kill",
                        lambda *a: pytest.fail("killed something unidentified"))
    assert menubar.evict_unsupervised(vault) is False


def test_a_loose_copy_hands_the_menu_bar_to_the_login_agent(fake_home,
                                                            monkeypatch):
    """The terminal copy dies with the terminal - SIGHUP takes the whole
    foreground process group - and nothing is watching to put it back."""
    _write_plist(fake_home)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    up = []
    fake = _launchctl()

    def starting(*argv):
        out = fake(*argv)
        if argv[0] == "kickstart":
            up.append(5150)                   # launchd started its copy
        return out
    monkeypatch.setattr(menubar, "_launchctl", starting)
    monkeypatch.setattr(menubar, "running_pids", lambda: list(up))
    assert menubar.hand_over_to_login_agent(str(fake_home / "memory.vault"),
                                            timeout=5) is True
    assert any(c[0] == "kickstart" for c in fake.calls), fake.calls


def test_the_handover_repairs_an_agent_that_was_never_loaded(fake_home,
                                                             monkeypatch):
    """The state a real machine is left in by a bootstrap that failed: the
    plist is on disk and launchd has never heard of it."""
    _write_plist(fake_home)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    up, loaded, calls = [], [], []

    def repairing(*argv):
        calls.append(argv)
        if argv[0] == "print":
            return (0, "") if loaded else (113, "Could not find service")
        if argv[0] == "bootstrap":
            loaded.append(True)               # now launchd has it...
            up.append(5150)                   # ...and RunAtLoad started it
        return 0, ""
    monkeypatch.setattr(menubar, "_launchctl", repairing)
    monkeypatch.setattr(menubar, "running_pids", lambda: list(up))
    assert menubar.hand_over_to_login_agent(str(fake_home / "memory.vault"),
                                            timeout=5) is True
    assert any(c[0] == "bootstrap" for c in calls), calls


def test_no_login_agent_means_nothing_is_turned_on_behind_the_user(fake_home,
                                                                   monkeypatch):
    """No plist is either a machine that never had start at login or a user
    who switched it off. Neither is ours to overrule."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setattr(menubar, "_launchctl",
                        lambda *a: pytest.fail("touched launchd"))
    assert menubar.hand_over_to_login_agent(
        str(fake_home / "memory.vault")) is False


def test_the_supervised_copy_does_not_hand_over_to_itself(fake_home,
                                                          monkeypatch):
    _write_plist(fake_home)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XPC_SERVICE_NAME", menubar.LAUNCH_AGENT_LABEL)
    monkeypatch.setattr(menubar, "_launchctl",
                        lambda *a: pytest.fail("touched launchd"))
    assert menubar.hand_over_to_login_agent(
        str(fake_home / "memory.vault")) is False


def test_a_handover_that_produced_no_icon_keeps_the_one_it_had(fake_home,
                                                               monkeypatch):
    """Never trade a copy nothing supervises for no copy at all."""
    _write_plist(fake_home)
    vault = str(fake_home / "memory.vault")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setattr(menubar, "_launchctl", _launchctl())
    monkeypatch.setattr(menubar, "running_pids", lambda: [])   # nothing came up
    # False is the contract: the caller keeps the menu bar and runs the UI.
    assert menubar.hand_over_to_login_agent(vault, timeout=0.5) is False


def test_run_applies_the_precedence_in_both_directions():
    """The two halves have to be wired into run() to mean anything: the
    supervised copy takes the menu bar, the unsupervised one hands it over."""
    src = (menubar.Path(menubar.__file__).read_text(encoding="utf-8"))
    body = src.split("def run(", 1)[1]
    assert "evict_unsupervised(vault_path)" in body
    assert "_is_launchd_managed()" in body
    assert "hand_over_to_login_agent(vault_path)" in body


# --- the install and the upgrade --------------------------------------------

class _Proc:
    """A child that is still running when the installer looks."""

    def __init__(self, status=None):
        self.status = status

    def wait(self, timeout=None):
        if self.status is None:
            raise subprocess.TimeoutExpired("panel", timeout)
        return self.status


def _darwin_install(monkeypatch, state="on", running=True, spawned=None):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_tray_app", lambda: type(
        "A", (), {"set_login": staticmethod(lambda v, vault=None: state)}))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: running)
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: (spawned.append(a) if spawned
                                         is not None else None) or _Proc())


def test_a_mac_install_does_not_start_a_second_copy(monkeypatch, capsys,
                                                    tmp_path):
    """RunAtLoad already started the supervised copy. The one this would
    spawn is the loose one, and it is the one that cannot survive the shell
    it was started from."""
    spawned = []
    _darwin_install(monkeypatch, state="on", running=True, spawned=spawned)
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    assert spawned == [], "the login agent's copy was already up"
    assert "look for the icon" in capsys.readouterr().out


def test_a_mac_install_still_starts_one_if_launchd_produced_nothing(
        monkeypatch, capsys, tmp_path):
    spawned = []
    _darwin_install(monkeypatch, state="on", running=False, spawned=spawned)
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    assert spawned, "an install with no icon is an install that failed"


def test_an_install_does_not_claim_an_app_that_exited(monkeypatch, capsys,
                                                      tmp_path):
    """Spawning is not starting."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_tray_app", lambda: type(
        "A", (), {"set_login": staticmethod(lambda v, vault=None: "failed: refused")}))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _Proc(1))
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    out = capsys.readouterr().out
    assert "exited immediately" in out
    assert "look for the icon" not in out


def test_an_upgrade_restarts_the_agent_through_launchd(monkeypatch, capsys,
                                                       tmp_path):
    """A SIGTERM races KeepAlive: launchd relaunches the copy that exited
    non-zero while the caller is starting its own replacement."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "restart_agent": staticmethod(lambda: True),
        "quit_running": staticmethod(
            lambda *a, **k: pytest.fail("killed the agent by hand"))}))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_start_status_bar_app",
                        lambda v: pytest.fail("started a second copy"))
    cli._restart_status_bar_app(str(tmp_path / "v.vault"))
    assert "new build" in capsys.readouterr().out


def test_an_upgrade_restarts_the_service_on_linux(monkeypatch, capsys,
                                                  tmp_path):
    """Same operation, different supervisor."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "restart_supervised": staticmethod(lambda: True),
        "quit_running": staticmethod(
            lambda *a, **k: pytest.fail("killed the service by hand"))}))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_start_status_bar_app",
                        lambda v: pytest.fail("started a second copy"))
    cli._restart_status_bar_app(str(tmp_path / "v.vault"))
    assert "new build" in capsys.readouterr().out


def test_an_upgrade_falls_back_when_there_is_no_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    started = []
    monkeypatch.setattr(cli, "_start_status_bar_app", started.append)
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "restart_agent": staticmethod(lambda: False),
        "quit_running": staticmethod(lambda *a, **k: True)}))
    cli._restart_status_bar_app(str(tmp_path / "v.vault"))
    assert started, "an upgrade with no agent still has to put the icon back"


def test_restart_agent_will_not_kickstart_a_job_launchd_does_not_have(
        monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(menubar, "_launchctl",
                        _launchctl(print=(113, "Could not find service")))
    assert menubar.restart_agent() is False


# --- Linux ------------------------------------------------------------------

def test_linux_autostart_switched_off_in_the_desktop_reads_as_off(monkeypatch,
                                                                  tmp_path):
    """GNOME Tweaks and KDE both rewrite this entry with Hidden=true rather
    than deleting it. Reading the filename alone called that "on"."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(systray, "_is_linux", lambda: True)
    systray.install_autostart_entry("/v/memory.vault")
    assert systray.login_status() == "on"

    path = systray.autostart_entry_path()
    path.write_text(path.read_text(encoding="utf-8").replace(
        "Hidden=false", "Hidden=true"), encoding="utf-8")
    assert systray.login_status() == "off"


def test_linux_a_disabled_gnome_autostart_reads_as_off(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(systray, "_is_linux", lambda: True)
    systray.install_autostart_entry("/v/memory.vault")
    path = systray.autostart_entry_path()
    path.write_text(path.read_text(encoding="utf-8").replace(
        "X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-enabled=false"),
        encoding="utf-8")
    assert systray.login_status() == "off"


# --- Windows ----------------------------------------------------------------

class _FakeWinreg:
    """Enough of winreg to exercise the Run key from any OS."""
    HKEY_CURRENT_USER = 0
    REG_SZ = 1

    class _Key:
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def __init__(self, store=None):
        self.store = {} if store is None else store

    def OpenKey(self, root, path):                       # noqa: N802
        return self._Key(self.store)

    CreateKey = OpenKey

    def SetValueEx(self, key, name, _r, _t, value):      # noqa: N802
        self.store[name] = value

    def QueryValueEx(self, key, name):                   # noqa: N802
        if name not in self.store:
            raise FileNotFoundError(name)
        return self.store[name], self.REG_SZ

    def DeleteValue(self, key, name):                    # noqa: N802
        self.store.pop(name, None)


def test_windows_run_entry_is_checked_by_value_and_not_only_by_name(
        monkeypatch):
    """A policy or a roaming profile can leave somebody else's command under
    our name. That is not this app starting at sign-in."""
    reg = _FakeWinreg()
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_winreg", lambda: reg)
    monkeypatch.setattr(systray, "_autostart_command",
                        lambda *a: '"C:\\ours\\compartment.exe" tray')

    real = reg.SetValueEx

    def hijack(key, name, r, t, _value):
        real(key, name, r, t, '"C:\\theirs\\other.exe" tray')
    monkeypatch.setattr(reg, "SetValueEx", hijack)
    assert systray.set_login(True).startswith("error")


def test_windows_a_good_write_still_reports_on(monkeypatch, tmp_path):
    exe = tmp_path / "compartment.exe"
    exe.write_text("", encoding="utf-8")
    reg = _FakeWinreg()
    _as_windows(monkeypatch, str(exe))
    monkeypatch.setattr(systray, "_winreg", lambda: reg)
    monkeypatch.setattr(systray, "_autostart_command", lambda *a: f'"{exe}" tray')
    assert systray.set_login(True) == "on"
    assert systray.login_status() == "on"


def test_windows_a_run_entry_naming_a_program_that_is_gone_is_not_on(
        monkeypatch, tmp_path):
    """An uninstalled Python or a moved virtualenv leaves the entry behind,
    and it starts nothing at the next sign-in."""
    reg = _FakeWinreg({systray.RUN_VALUE: f'"{tmp_path / "gone.exe"}" tray'})
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_winreg", lambda: reg)
    assert systray.login_status() != "on"


def test_windows_can_see_a_running_panel(monkeypatch):
    """It used to answer "nothing is running" on Windows whatever was
    running, which is worse than not asking: the install waited fifteen
    seconds for an app that was already up, then started a second one."""
    _as_windows(monkeypatch)
    rows = ('"compartment.exe","8412","Console","1","54,321 K"\n'
            f'"compartment.exe","{os.getpid()}","Console","1","54,321 K"\n')
    monkeypatch.setattr(systray.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": rows})())
    assert systray.running_pids() == [8412], "and never this process"


def test_windows_reads_no_pid_out_of_tasklists_no_match_line(monkeypatch):
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "INFO: No tasks are running which match the "
                            "specified criteria.\n"})())
    assert systray.running_pids() == []


def test_windows_installs_do_not_start_through_the_scheduler(monkeypatch):
    """`schtasks /run` starts the task in whatever session Task Scheduler
    picks, and a tray icon in another session is one nobody can see. The
    task is for the next sign-in; the install starts a copy it can show."""
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: pytest.fail("ran the task to install"))
    assert systray.start_supervised() is False


def test_an_install_does_not_call_a_copy_standing_down_a_failure(monkeypatch,
                                                                 tmp_path,
                                                                 capsys):
    """Exit zero is how a second copy hands over when one is already there.
    Reporting that as "the app exited immediately" told a Windows user their
    install had failed when the icon was sitting in front of them."""
    monkeypatch.setattr(sys, "platform", "win32")
    # cli reaches shutil.which too, and from 3.12 that asks _winapi whenever
    # sys.platform says win32 - see _as_windows.
    monkeypatch.setattr(cli, "shutil", type("S", (), {
        "which": staticmethod(lambda n: WINDOWS_EXE)}))
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "set_login": staticmethod(lambda on, vault=None: "on"),
        "start_supervised": staticmethod(lambda: False)}))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _Proc(0))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: True)
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    out = capsys.readouterr().out
    assert "exited immediately" not in out
    assert "app started" in out


def test_an_install_still_says_so_when_nothing_is_running(monkeypatch,
                                                          tmp_path, capsys):
    """Exit zero with no copy up is the one case that really is a failure."""
    monkeypatch.setattr(sys, "platform", "win32")
    # cli reaches shutil.which too, and from 3.12 that asks _winapi whenever
    # sys.platform says win32 - see _as_windows.
    monkeypatch.setattr(cli, "shutil", type("S", (), {
        "which": staticmethod(lambda n: WINDOWS_EXE)}))
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "set_login": staticmethod(lambda on, vault=None: "on"),
        "start_supervised": staticmethod(lambda: False)}))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _Proc(0))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: False)
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    assert "not running" in capsys.readouterr().out


def test_windows_quit_running_reports_what_taskkill_said(monkeypatch):
    """It returned True whatever happened, so an update could be told the old
    build had gone while it was still holding the lock."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(systray.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())
    assert systray.quit_running() is False
    monkeypatch.setattr(systray.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    assert systray.quit_running() is True


# --- Linux supervision: a systemd user service ------------------------------

def _fake_systemctl(**answers):
    """A user manager that answers the way systemctl does: the query verbs
    print a word, and the default is the healthy one."""
    defaults = {"is-enabled": (0, "enabled"), "is-active": (0, "active"),
                "is-system-running": (0, "running")}
    calls = []

    def fake(*argv):
        calls.append(argv)
        key = argv[0].replace("_", "-")
        return answers.get(argv[0], defaults.get(key, (0, "")))
    fake.calls = calls
    return fake


@pytest.fixture()
def as_linux(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(systray, "_is_linux", lambda: True)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    return tmp_path


def test_the_unit_restarts_a_panel_that_died_and_not_one_that_left(as_linux):
    """The Linux half of KeepAlive SuccessfulExit=false. Restart=always would
    fight the Quit button; no Restart at all is what it had."""
    text = systray.systemd_unit_text("/v/memory.vault")
    assert "Restart=on-failure" in text
    assert "Restart=always" not in text
    assert "WantedBy=graphical-session.target" in text
    assert "/v/memory.vault" in text


def test_the_unit_gives_up_on_a_machine_it_can_never_draw_on(as_linux):
    """A user manager with no display would otherwise restart it for ever."""
    text = systray.systemd_unit_text()
    assert "StartLimitBurst=" in text
    # These moved to [Unit] in systemd 229; in [Service] they are ignored
    # with a warning, which is the same as not having written them.
    unit = text.split("[Service]", 1)[0]
    assert "StartLimitBurst=" in unit and "StartLimitIntervalSec=" in unit


def test_installing_the_unit_enables_it_and_checks(as_linux, monkeypatch):
    fake = _fake_systemctl()
    monkeypatch.setattr(systray, "_systemctl", fake)
    assert systray.install_systemd_unit("/v/memory.vault") == "on"
    assert systray.systemd_unit_path().is_file()
    verbs = [c[0] for c in fake.calls]
    assert "daemon-reload" in verbs and "enable" in verbs
    assert "is-enabled" in verbs, "enabling is a request, not a result"


def test_a_unit_systemd_refuses_is_reported_as_a_failure(as_linux,
                                                          monkeypatch):
    monkeypatch.setattr(systray, "_systemctl", _fake_systemctl(
        enable=(1, "Failed to enable unit: Unit is masked.")))
    out = systray.install_systemd_unit()
    assert out.startswith("error") and "masked" in out


def test_a_unit_that_enables_but_does_not_stay_enabled_is_a_failure(
        as_linux, monkeypatch):
    monkeypatch.setattr(systray, "_systemctl",
                        _fake_systemctl(**{"is-enabled": (1, "disabled")}))
    assert systray.install_systemd_unit().startswith("error")


def test_login_status_asks_systemd_once_there_is_a_unit(as_linux,
                                                        monkeypatch):
    monkeypatch.setattr(systray, "_systemctl", _fake_systemctl())
    systray.install_systemd_unit()
    assert systray.login_status() == "on"
    # The unit is on disk and systemd does not have it: the Linux version of
    # a plist that was never loaded.
    monkeypatch.setattr(systray, "_systemctl",
                        _fake_systemctl(**{"is-enabled": (1, "disabled")}))
    assert systray.login_status() != "on"


def test_a_supervised_linux_install_has_exactly_one_starter(as_linux,
                                                            monkeypatch):
    """Two starters means one copy loses the lock and exits zero, and exit
    zero is the one exit Restart=on-failure is built to leave alone."""
    monkeypatch.setattr(systray, "systemd_available", lambda: True)
    monkeypatch.setattr(systray, "_systemctl", _fake_systemctl())
    assert systray.set_login(True) == "on"
    assert systray.systemd_unit_path().is_file()
    assert not systray.autostart_entry_path().exists(), "two starters"
    assert systray.desktop_entry_path().is_file(), "the menu entry is not one"


def test_a_machine_with_no_user_manager_still_starts_at_login(as_linux,
                                                              monkeypatch):
    monkeypatch.setattr(systray, "systemd_available", lambda: False)
    assert systray.set_login(True) == "on"
    assert systray.autostart_entry_path().is_file()
    assert not systray.systemd_unit_path().exists()


def test_a_unit_that_will_not_run_is_undone_rather_than_left_half_done(
        as_linux, monkeypatch):
    """Enabled but not active proves nothing. Rather than leave a unit that
    may never start and no autostart entry either, put the entry back."""
    monkeypatch.setattr(systray, "systemd_available", lambda: True)
    monkeypatch.setattr(systray, "_systemctl",
                        _fake_systemctl(**{"is-active": (3, "inactive")}))
    assert systray.set_login(True) == "on"
    assert systray.autostart_entry_path().is_file()
    assert not systray.systemd_unit_path().exists()


def test_turning_it_off_takes_the_unit_with_it(as_linux, monkeypatch):
    monkeypatch.setattr(systray, "systemd_available", lambda: True)
    fake = _fake_systemctl()
    monkeypatch.setattr(systray, "_systemctl", fake)
    systray.set_login(True)
    monkeypatch.setattr(systray, "_systemctl",
                        _fake_systemctl(**{"is-enabled": (1, "disabled")}))
    assert systray.set_login(False) == "off"
    assert not systray.systemd_unit_path().exists()


def test_a_loose_linux_copy_hands_over_to_the_service(as_linux, monkeypatch):
    monkeypatch.setattr(systray, "_systemctl", _fake_systemctl())
    systray.install_systemd_unit()
    up = []
    base = _fake_systemctl()

    def starting(*argv):
        if argv[0] == "start":
            up.append(6060)
        return base(*argv)
    monkeypatch.setattr(systray, "_systemctl", starting)
    monkeypatch.setattr(systray, "running_pids", lambda: list(up))
    assert systray.hand_over_to_supervisor(
        str(as_linux / "memory.vault"), timeout=5) is True


def test_the_service_copy_does_not_hand_over_to_itself(as_linux, monkeypatch):
    """systemd sets INVOCATION_ID for everything it runs."""
    monkeypatch.setenv("INVOCATION_ID", "aaf1e6a0")
    monkeypatch.setattr(systray, "_systemctl",
                        lambda *a: pytest.fail("talked to systemd"))
    assert systray.hand_over_to_supervisor(str(as_linux / "v.vault")) is False


def test_no_unit_means_nothing_is_enabled_behind_the_user(as_linux,
                                                          monkeypatch):
    monkeypatch.setattr(systray, "_systemctl",
                        lambda *a: pytest.fail("talked to systemd"))
    assert systray.hand_over_to_supervisor(str(as_linux / "v.vault")) is False


# --- Windows supervision: a scheduled task ----------------------------------

WINDOWS_EXE = "C:\\Program Files\\Compartment\\compartment.exe"


def _as_windows(monkeypatch, exe=WINDOWS_EXE):
    """Windows, from any machine.

    `shutil` is replaced as well as sys.platform, and not for convenience.
    From Python 3.12 `shutil.which` asks `_winapi` whenever sys.platform
    says win32, and `_winapi` is None everywhere else - so the real function
    raises AttributeError on the two platforms these tests actually run on,
    for code that has nothing wrong with it.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(systray, "_is_linux", lambda: False)
    monkeypatch.setattr(systray, "shutil",
                        type("S", (), {"which": staticmethod(lambda n: exe)}))
    return exe


def test_the_task_restarts_a_panel_that_failed(monkeypatch):
    _as_windows(monkeypatch)
    xml = systray.scheduled_task_xml("C:\\v\\memory.vault")
    assert "<RestartOnFailure>" in xml and "<Count>3</Count>" in xml
    assert "<LogonTrigger>" in xml
    # Without an interactive token there is no desktop to draw an icon on.
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    # A time limit would have Task Scheduler kill a panel after three days.
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml


def test_the_task_carries_the_vault_and_splits_the_command(monkeypatch):
    _as_windows(monkeypatch)
    xml = systray.scheduled_task_xml("C:\\v\\memory.vault")
    assert "C:\\v\\memory.vault" in xml
    assert "<Command>" in xml and "<Arguments>" in xml
    # The command element is a program, never a whole command line.
    command = xml.split("<Command>")[1].split("</Command>")[0]
    assert " tray" not in command and not command.startswith('"')


def test_the_task_xml_escapes_what_xml_cannot_hold(monkeypatch):
    _as_windows(monkeypatch)
    xml = systray.scheduled_task_xml("C:\\Users\\A & B\\memory.vault")
    assert "A &amp; B" in xml and "A & B" not in xml


def test_a_task_the_scheduler_refuses_is_reported_as_a_failure(monkeypatch):
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: (1, "ERROR: Access is denied."))
    out = systray.install_scheduled_task()
    assert out.startswith("error") and "Access is denied" in out


def test_a_task_that_did_not_survive_being_created_is_a_failure(monkeypatch):
    """Creating and querying are different questions."""
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_schtasks", lambda *a: (
        (0, "") if a[0] == "/create" else (1, "ERROR: cannot find the file")))
    assert systray.install_scheduled_task().startswith("error")


def test_windows_prefers_the_task_and_drops_the_run_key(monkeypatch):
    """Two starters at sign-in means one exits zero, which is the exit the
    restart setting is built to leave alone."""
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_schtasks", lambda *a: (0, ""))
    dropped = []
    monkeypatch.setattr(systray, "_delete_run_value",
                        lambda: dropped.append(True))
    monkeypatch.setattr(systray, "_winreg",
                        lambda: pytest.fail("wrote a Run key as well"))
    assert systray.set_login(True) == "on"
    assert dropped == [True]


def test_windows_falls_back_to_the_run_key(monkeypatch, tmp_path):
    """A locked-down machine that will not take a task still starts at
    sign-in, just without being put back if it dies."""
    _as_windows(monkeypatch)
    exe = tmp_path / "compartment.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: (1, "ERROR: Access is denied."))
    reg = _FakeWinreg()
    monkeypatch.setattr(systray, "_winreg", lambda: reg)
    monkeypatch.setattr(systray, "_autostart_command", lambda *a: f'"{exe}" tray')
    assert systray.set_login(True) == "on"
    assert systray.RUN_VALUE in reg.store


def test_windows_status_asks_the_scheduler_first(monkeypatch):
    _as_windows(monkeypatch)
    monkeypatch.setattr(systray, "_schtasks", lambda *a: (0, ""))
    monkeypatch.setattr(systray, "_winreg",
                        lambda: pytest.fail("read the Run key instead"))
    assert systray.login_status() == "on"


def test_turning_it_off_removes_the_task(monkeypatch):
    _as_windows(monkeypatch)
    calls = []
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: (calls.append(a), (1, "not found"))[1])
    monkeypatch.setattr(systray, "_winreg", lambda: _FakeWinreg())
    assert systray.set_login(False) == "off"
    assert any(c[0] == "/delete" for c in calls), calls


# --- outliving the terminal it was typed in ---------------------------------

def test_a_copy_typed_at_a_prompt_knows_it_is_tied_to_one(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.delenv(menubar.DETACHED_ENV, raising=False)
    monkeypatch.delenv(menubar.FOREGROUND_ENV, raising=False)
    monkeypatch.setattr(menubar.sys, "stdin", Tty())
    monkeypatch.setattr(menubar.sys, "stdout", Tty())
    monkeypatch.setattr(menubar.sys, "stderr", Tty())
    assert menubar.started_from_a_terminal() is True


def test_the_detached_copy_never_detaches_again(monkeypatch):
    """Otherwise every launch would spawn another launch, for ever."""
    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(menubar.sys, "stdin", Tty())
    monkeypatch.setenv(menubar.DETACHED_ENV, "1")
    assert menubar.started_from_a_terminal() is False


def test_a_foreground_override_is_honoured(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.delenv(menubar.DETACHED_ENV, raising=False)
    monkeypatch.setattr(menubar.sys, "stdin", Tty())
    monkeypatch.setenv(menubar.FOREGROUND_ENV, "1")
    assert menubar.started_from_a_terminal() is False


def test_a_launchd_copy_is_not_a_terminal_copy(monkeypatch):
    monkeypatch.delenv(menubar.DETACHED_ENV, raising=False)
    monkeypatch.delenv(menubar.FOREGROUND_ENV, raising=False)
    monkeypatch.setattr(menubar.sys, "stdin", None)
    monkeypatch.setattr(menubar.sys, "stdout", None)
    monkeypatch.setattr(menubar.sys, "stderr", None)
    assert menubar.started_from_a_terminal() is False


def test_the_relaunch_leaves_the_terminals_session(tmp_path, monkeypatch):
    """`start_new_session` is the whole point: a session of its own has no
    controlling terminal, so the window's SIGHUP cannot reach it."""
    seen = {}
    up = []

    def fake_popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        up.append(7070)
        return object()
    # Not the real one: it resolves the CLI by asking the login shell, and
    # this test has replaced the Popen that would run it.
    monkeypatch.setattr(menubar, "compartment_bin", lambda: "/nowhere/compartment")
    monkeypatch.setattr(menubar.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(menubar, "running_pids", lambda: list(up))
    vault = str(tmp_path / "memory.vault")
    assert menubar.relaunch_detached(vault, timeout=5) is True
    if sys.platform == "win32":
        # DETACHED_PROCESS: Windows has no sessions in the POSIX sense, and
        # this is what cuts the same tie to the console.
        assert seen["kwargs"].get("creationflags") == 0x00000008
    else:
        assert seen["kwargs"].get("start_new_session") is True
    assert seen["kwargs"]["env"][menubar.DETACHED_ENV] == "1"
    assert seen["argv"][-1] == "menubar" and vault in seen["argv"]


def test_the_relaunch_keeps_the_name_the_app_had(tmp_path, monkeypatch):
    """Through the console script, not `python -m`. Windows finds this app
    by image name - Get-Process, tasklist, taskkill /IM all do - so a copy
    relaunched as python.exe is running and cannot be found or stopped."""
    seen, up = {}, []
    exe = tmp_path / "compartment"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(menubar, "compartment_bin", lambda: str(exe))
    monkeypatch.setattr(menubar.subprocess, "Popen",
                        lambda argv, **k: (seen.update(argv=argv),
                                           up.append(1), object())[2])
    monkeypatch.setattr(menubar, "running_pids", lambda: list(up))
    menubar.relaunch_detached(str(tmp_path / "memory.vault"), timeout=5)
    assert seen["argv"][0] == str(exe)
    assert "-m" not in seen["argv"], "relaunched as the interpreter"


def test_the_install_never_hands_its_child_a_terminal(monkeypatch, tmp_path):
    """The spawned copy inherits stdin unless told otherwise, sees a tty on
    the other end, and detaches itself - so the copy the installer started
    exits and hands over to one the installer never hears about."""
    seen = {}
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cli, "_linux_gui_available", lambda: True)
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "set_login": staticmethod(lambda on, vault=None: "on"),
        "start_supervised": staticmethod(lambda: False)}))
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda a, **k: (seen.update(k), _Proc())[1])
    cli._start_status_bar_app(str(tmp_path / "v.vault"))
    assert seen.get("stdin") == cli.subprocess.DEVNULL


def test_the_relaunch_keeps_the_panel_if_nothing_came_up(tmp_path,
                                                          monkeypatch):
    """Never trade a copy tied to a terminal for no copy at all."""
    monkeypatch.setattr(menubar, "compartment_bin", lambda: "/nowhere/compartment")
    monkeypatch.setattr(menubar.subprocess, "Popen",
                        lambda *a, **k: object())
    monkeypatch.setattr(menubar, "running_pids", lambda: [])
    assert menubar.relaunch_detached(str(tmp_path / "memory.vault"),
                                     timeout=0.5) is False
    assert menubar._INSTANCE_LOCK is not None


def test_run_detaches_from_the_terminal(monkeypatch):
    """Wired into run(), after the handover and before the window."""
    src = menubar.Path(menubar.__file__).read_text(encoding="utf-8")
    body = src.split("def run(", 1)[1]
    assert "started_from_a_terminal()" in body
    assert "relaunch_detached(vault_path, show)" in body
    tray = (menubar.Path(systray.__file__).read_text(encoding="utf-8")
            .split("def run(", 1)[1])
    assert "hand_over_to_supervisor(vault_path)" in tray
    # And deliberately NOT the detach. `compartment tray` and `compartment
    # panel` are expected to BE the running app: whoever started one holds
    # its handle, and a copy that re-launched itself and exited zero is
    # indistinguishable, from there, from the app dying on its own.
    assert "relaunch_detached" not in tray


# --- the vault the user actually asked for ----------------------------------
# `_autostart_command` and `install_autostart_entry` both take a vault and
# take care to keep it, and `set_login` called them with nothing. So a user
# running a second vault registered start-at-login against the default one
# and got the wrong memory back at every sign-in, on all three systems.

def test_the_login_agent_opens_the_vault_it_was_given(fake_home, monkeypatch):
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/bin/true"])
    monkeypatch.setattr(menubar, "_launchctl", _launchctl())
    menubar.set_login(True, "/somewhere/else/memory.vault")
    import plistlib
    data = plistlib.loads((fake_home / "Library" / "LaunchAgents"
                           / f"{menubar.LAUNCH_AGENT_LABEL}.plist"
                           ).read_bytes())
    assert (data["EnvironmentVariables"]["COMPARTMENT_VAULT"]
            == "/somewhere/else/memory.vault")


def test_the_plist_survives_an_ampersand_in_a_path(fake_home, monkeypatch):
    """It is XML. An unescaped & makes a plist launchd cannot parse, and it
    fails at the next login rather than when it was set - so it applies to
    the launcher path as much as to the vault."""
    import plistlib
    monkeypatch.setattr(menubar, "_launcher_argv",
                        lambda: ["/Users/a/R & D/Compartment.app/x"])
    monkeypatch.setattr(menubar, "_launchctl", _launchctl())
    menubar.set_login(True, "/Users/a/R & D/memory.vault")
    data = plistlib.loads((fake_home / "Library" / "LaunchAgents"
                           / f"{menubar.LAUNCH_AGENT_LABEL}.plist"
                           ).read_bytes())
    assert (data["EnvironmentVariables"]["COMPARTMENT_VAULT"]
            == "/Users/a/R & D/memory.vault")
    assert data["ProgramArguments"] == ["/Users/a/R & D/Compartment.app/x"]


def test_the_linux_entries_carry_the_vault_they_were_given(as_linux,
                                                           monkeypatch):
    monkeypatch.setattr(systray, "systemd_available", lambda: False)
    systray.set_login(True, "/home/x/second.vault")
    assert "/home/x/second.vault" in systray.autostart_entry_path().read_text()
    assert "/home/x/second.vault" in systray.desktop_entry_path().read_text()


def test_the_unit_carries_the_vault_it_was_given(as_linux, monkeypatch):
    monkeypatch.setattr(systray, "systemd_available", lambda: True)
    monkeypatch.setattr(systray, "_systemctl", _fake_systemctl())
    systray.set_login(True, "/home/x/second.vault")
    assert "/home/x/second.vault" in systray.systemd_unit_path().read_text()


def test_the_scheduled_task_carries_the_vault_it_was_given(monkeypatch):
    _as_windows(monkeypatch)
    seen = {}

    def fake(*argv):
        if argv[0] == "/create":
            seen["xml"] = Path(argv[argv.index("/xml") + 1]).read_text(
                encoding="utf-16")
        return 0, ""
    monkeypatch.setattr(systray, "_schtasks", fake)
    systray.install_scheduled_task("C:\\second\\memory.vault")
    assert "C:\\second\\memory.vault" in seen["xml"]


def test_the_run_key_carries_the_vault_it_was_given(monkeypatch, tmp_path):
    exe = tmp_path / "compartment.exe"
    exe.write_text("", encoding="utf-8")
    _as_windows(monkeypatch, str(exe))
    reg = _FakeWinreg()
    monkeypatch.setattr(systray, "_winreg", lambda: reg)
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: (1, "ERROR: Access is denied."))
    systray.set_login(True, "C:\\second\\memory.vault")
    assert "C:\\second\\memory.vault" in reg.store[systray.RUN_VALUE]


def test_the_install_registers_the_vault_it_is_installing(monkeypatch,
                                                          tmp_path):
    """`init --vault X` must not register start-at-login for the default."""
    seen = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_tray_app", lambda: type("A", (), {
        "set_login": staticmethod(
            lambda on, vault=None: (seen.append(vault), "on")[1]),
        "start_supervised": staticmethod(lambda: True)}))
    monkeypatch.setattr(cli, "_wait_for_status_bar_app", lambda *a, **k: True)
    vault = str(tmp_path / "second.vault")
    cli._start_status_bar_app(vault)
    assert seen == [vault]


# --- the deliberate behaviour that must survive all of this -----------------

def test_quit_still_means_gone_until_the_next_login(fake_home, monkeypatch):
    """KeepAlive SuccessfulExit=false is the whole reason the Quit button
    works. Nothing here may turn it into a plain restart-always."""
    monkeypatch.setattr(menubar, "_launcher_argv", lambda: ["/bin/true"])
    monkeypatch.setattr(menubar, "_launchctl", _launchctl())
    menubar._set_login_agent(True)
    import plistlib
    data = plistlib.loads((fake_home / "Library" / "LaunchAgents"
                           / f"{menubar.LAUNCH_AGENT_LABEL}.plist"
                           ).read_bytes())
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["RunAtLoad"] is True
