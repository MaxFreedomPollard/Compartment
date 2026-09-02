"""macOS menu bar app: Compartment in the status bar.

Click the icon and a popover shows what memory is doing - whether the vault
is open, the three settings worth changing day to day, and the last handful
of things it remembered. No dock icon, no window to manage.

Design notes:

* The data layer is plain functions with no AppKit in sight, so it is
  testable on every OS in CI. AppKit is imported inside `run()`, which is the
  only part that cannot run headless.
* State is read by shelling out to the `compartment` CLI rather than opening the
  vault in-process. A status bar app that idles at 300 MB because it is
  holding an embedding model would be a bad neighbour; a subprocess that
  exits is not.
* Settings live in `<vault>.config.json`, which needs no passphrase, so the
  toggles work whether or not the vault is currently unlocked.
"""
from __future__ import annotations

from .home import env, home
import errno
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__
from .acl import VaultConfig

AUTO_LOCK_CHOICES = [15, 30, 60, 0]          # 0 = never
POPOVER_WIDTH = 360
POPOVER_MAX_HEIGHT = 640
CONTENT_INSET = 16
CONTENT_WIDTH = POPOVER_WIDTH - CONTENT_INSET * 2
RECENT_COUNT = 5

# NSLayoutAttribute. Named because passing 0 here silently centres everything
# instead of raising - the layout looks "almost right" and nothing tells you.
_LEADING = 5
_CENTER_Y = 10
_HORIZONTAL, _VERTICAL = 0, 1

# Written beside the vault the first time the app runs, so the very first
# launch can open the panel by itself instead of leaving the user hunting a
# menu bar icon they have never seen before.
FIRST_RUN_MARKER = ".menubar-introduced"

# Menu bar icons are 18pt tall by convention; the asset is drawn at @2x.
MENUBAR_POINTS = 18

# A second launch asks the copy already running to show itself, over this.
SHOW_NOTIFICATION = "io.github.maxfreedompollard.compartment.show"


def claim_first_run(vault: str) -> bool:
    """True exactly once, on the first launch. Never raises: a read-only home
    directory should cost the user a nicety, not the app."""
    try:
        marker = Path(vault).expanduser().parent / FIRST_RUN_MARKER
        if marker.exists():
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        return True
    except OSError:
        return False


# --------------------------------------------------------------- data layer

#: Held for as long as the status bar app runs. One icon per vault.
INSTANCE_LOCK_NAME = ".menubar.lock"

#: Module-level so the handle outlives acquire_instance_lock's frame. A
#: garbage-collected file object closes its descriptor, and closing the
#: descriptor drops the lock, which would let a second copy in.
_INSTANCE_LOCK = None


def acquire_instance_lock(vault: str):
    """Claim the right to be the status bar app for this vault.

    Returns (handle, is_only_copy). An operating system lock rather than a
    pid file, because the kernel drops it when the process dies: a pid file
    left behind by a crash or a kill would lock the app out of its own menu
    bar until someone deleted the file by hand.

    A machine where the lock cannot be taken at all - a read-only home, an
    exotic filesystem - is told it is the only copy. Starting twice is a
    nuisance; refusing to start is a broken install, and the nuisance is the
    better failure.
    """
    global _INSTANCE_LOCK
    try:
        path = Path(vault).expanduser().parent / INSTANCE_LOCK_NAME
    except (OSError, ValueError):
        return None, True

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")                       # noqa: SIM115 - held open
    except OSError:
        return None, True
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        # Contested is the only reason to stand down. Anything else means
        # the lock could not be taken at all, and refusing to start over
        # that would trade a spare icon for no icon.
        contested = exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK,
                                  errno.EWOULDBLOCK)
        return (None, False) if contested else (None, True)
    except Exception:                # noqa: BLE001 - no locking here at all
        return fh, True
    _INSTANCE_LOCK = fh
    _record_lock_holder(fh)
    return fh, True


def _record_lock_holder(fh) -> None:
    """Leave our pid in the lock file.

    So that the copy entitled to take the menu bar knows which single process
    to ask for it. The lock is per vault on purpose - a second vault is a
    second memory and gets an icon of its own - and a pgrep for every running
    copy would take that one down too.

    POSIX only. msvcrt locks the first byte outright, and another process
    cannot read a byte range Windows has locked, so on Windows the file would
    be unreadable by the only reader that wants it.
    """
    if sys.platform == "win32":
        return
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n".encode())
        fh.flush()
    except OSError:
        pass


def lock_holder_pid(vault: str) -> int | None:
    """The pid of the copy holding this vault's menu bar, if it said so.

    None when the file is empty, which is what every lock written before the
    pid was recorded looks like. That is a reason to leave the incumbent
    alone, never a reason to guess at it.
    """
    try:
        path = Path(vault).expanduser().parent / INSTANCE_LOCK_NAME
        pid = int((path.read_text(encoding="utf-8", errors="replace")
                   ).strip() or 0)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and pid != os.getpid() else None


def release_instance_lock() -> None:
    """Give the lock up deliberately, for a handover to a copy that will
    outlive this one. The kernel does this on exit anyway; doing it first
    means the other copy is not left waiting on a process that is leaving."""
    global _INSTANCE_LOCK
    fh, _INSTANCE_LOCK = _INSTANCE_LOCK, None
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass


def compartment_bin() -> str:
    """A real `compartment` CLI path, for commands handed to other programs.

    Inside Compartment.app the executable sits next to a launcher called `Compartment`,
    and macOS filesystems are case-insensitive by default - so looking for
    "compartment" beside the interpreter finds the launcher and the app ends up
    invoking *itself* instead of the CLI. Prefer the console script in the
    environment, and never accept anything inside a bundle's MacOS folder.
    """
    candidates = [Path(sys.prefix) / "bin" / "compartment",
                  Path(sys.executable).parent / "compartment"]
    for c in candidates:
        if c.is_file() and c.parent.name != "MacOS":
            return str(c)
    # Same PATH problem as user_path() describes: from a login item, a bare
    # which() misses ~/.local/bin and writes "compartment" into the agent's
    # config, which then only works when the agent happens to be started
    # from a shell that has it.
    return (shutil.which("compartment", path=user_path())
            or shutil.which("compartment") or "compartment")


def _cli_argv() -> list[str]:
    """How this process runs the CLI.

    NOT sys.executable. Inside the .app that is the bundle launcher, a small
    binary that always starts `compartment.cli menubar` and appends whatever
    else it was given - so every CLI call the panel made came back as an
    argparse usage error. The panel then read the word "keyfile" out of that
    usage line and told the user their vault needed a 2FA keyfile it had
    never had.

    sys.prefix is the real interpreter's home in every case that matters:
    PYTHONHOME points it at Contents/Resources/runtime inside the bundle, and
    at the venv or system prefix everywhere else."""
    exe = Path(sys.prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    if exe.exists():
        return [str(exe), "-m", "compartment.cli"]
    return [sys.executable, "-m", "compartment.cli"]


def default_vault() -> str:
    return env("VAULT", str(home() / "memory.vault"))


#: Worked out once, then reused: asking the login shell costs a fork.
_USER_PATH = None


def user_path() -> str:
    """The PATH a terminal on this machine would have.

    A status bar app started at login inherits launchd's PATH, which is
    /usr/bin:/bin:/usr/sbin:/sbin and nothing else. Every agent CLI worth
    wiring lives somewhere else: `claude` installs into ~/.local/bin,
    Homebrew into /opt/homebrew/bin. So `integrate` run from the app could
    not find the very tool it was there to connect, skipped the part that
    needed it, and reported the agent as not installed - on a machine where
    it plainly was.

    The login shell is asked because it is the only thing that knows what
    the user's own PATH is. The well-known directories are added after it as
    a floor, for the case where the shell cannot be asked at all.
    """
    global _USER_PATH
    if _USER_PATH is not None:
        return _USER_PATH
    parts = []
    if sys.platform != "win32":
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            out = subprocess.run([shell, "-lc", 'printf %s "$PATH"'],
                                 capture_output=True, text=True, timeout=15,
                                 encoding="utf-8", errors="replace").stdout
            parts += [p for p in (out or "").strip().split(os.pathsep) if p]
        except (OSError, subprocess.SubprocessError):
            pass
    parts += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if sys.platform != "win32":
        h = Path.home()
        parts += [str(h / ".local" / "bin"), "/usr/local/bin",
                  "/opt/homebrew/bin", str(h / "bin"),
                  str(h / ".claude" / "local")]
    seen, ordered = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    _USER_PATH = os.pathsep.join(ordered)
    return _USER_PATH


def _run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           env={**os.environ, "PATH": user_path()})
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _json_cmd(vault: str, *sub: str) -> dict | None:
    """Run a CLI subcommand and parse the JSON it prints.

    Its own runner, not `_run`: that one folds stderr into the output for
    error reporting, and anything a library says on stderr then lands after
    the JSON and breaks the parse. onnxruntime does exactly that on Azure -
    its device discovery warns about Hyper-V's PCI paths - so the panel on
    such a machine read a healthy, unlocked vault as "could not read vault
    status" and showed it locked. Only stdout is the CLI's answer.

    raw_decode rather than loads for the same reason at the other end: it
    stops at the end of the first JSON value, so noise after the payload -
    whatever prints it - can never invalidate the payload itself.
    """
    try:
        p = subprocess.run([*_cli_argv(), "--vault", vault, *sub],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PATH": user_path()})
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out = p.stdout or ""
    start = out.find("{")
    if start < 0:
        return None
    try:
        doc, _ = json.JSONDecoder().raw_decode(out[start:])
        return doc
    except json.JSONDecodeError:
        return None


def read_settings(vault: str) -> dict:
    """The three adjustable settings, plus whether the capture hook is on.

    Reads the config file directly: no vault unlock, no passphrase, so the
    popover works even when memory is locked.
    """
    cfg = VaultConfig.load(vault)
    from . import claude_hooks
    try:
        hook = claude_hooks.is_installed()
    except Exception:                                   # noqa: BLE001
        hook = False
    return {
        "capture_hook": hook,
        "search_starter_facts": bool(
            cfg.settings.get("search_starter_facts", True)),
        "expire_memories": bool(cfg.settings.get("expire_memories", True)),
        "auto_lock_minutes": int(cfg.settings.get("auto_lock_minutes", 30)),
    }


def set_setting(vault: str, key: str, value) -> dict:
    """Change one setting. Returns the settings as they now stand."""
    if key == "capture_hook":
        from . import claude_hooks
        if value:
            claude_hooks.install(compartment_bin=compartment_bin(), vault=vault)
        else:
            claude_hooks.uninstall()
        return read_settings(vault)
    cfg = VaultConfig.load(vault)
    if key == "search_starter_facts":
        cfg.settings["search_starter_facts"] = bool(value)
    elif key == "expire_memories":
        cfg.settings["expire_memories"] = bool(value)
    elif key == "auto_lock_minutes":
        cfg.settings["auto_lock_minutes"] = int(value)
    else:
        raise KeyError(f"unknown setting {key!r}")
    cfg.save(vault)
    return read_settings(vault)


def fetch_state(vault: str) -> dict:
    """Everything the popover shows. Never raises - a status bar app that
    dies because a vault is missing is worse than one that says so."""
    state = {"vault": vault, "exists": os.path.exists(vault), "locked": True,
             "records": 0, "organic": 0, "recent": [], "error": None,
             "integrations": integration_status(vault),
             "settings": {"capture_hook": False,
                          "search_starter_facts": True,
                          "expire_memories": True,
                          "auto_lock_minutes": 30}}
    try:
        state["settings"] = read_settings(vault)
    except Exception as exc:                            # noqa: BLE001
        state["error"] = str(exc)
    if not state["exists"]:
        state["error"] = ("no vault yet - choose a passphrase below to "
                          "create one")
        return state

    status = _json_cmd(vault, "status")
    if status is None:
        state["error"] = "could not read vault status"
        return state
    state["locked"] = bool(status.get("locked", True))
    state["records"] = int(status.get("records", 0) or 0)
    state["organic"] = int(status.get("organic_records", 0) or 0)
    if state["locked"]:
        return state

    recent = _json_cmd(vault, "recent", "--limit", str(RECENT_COUNT), "--json")
    if recent:
        counts = recent.get("counts") or {}
        state["organic"] = int(counts.get("organic", state["organic"]))
        state["records"] = int(counts.get("total", state["records"]))
        # newest first reads better in a list you glance at
        state["recent"] = list(reversed(recent.get("results") or []))
    return state


#: The `compartment dash` started from the panel, if any. One per panel
#: process: a second click reopens the same page instead of starting a second
#: server with a second random token for the same vault.
_DASH: dict = {"proc": None, "url": None}
_DASH_PREFIX = "Compartment dashboard: "


def dashboard_url(line: str) -> str | None:
    """The URL `compartment dash` announces on its first line, or None."""
    line = line.strip()
    if line.startswith(_DASH_PREFIX):
        return line[len(_DASH_PREFIX):].strip() or None
    return None


def _drain(proc) -> None:
    # `dash` says a few more lines over its life; read them so it can never
    # block on a full pipe.
    try:
        for _ in proc.stdout:
            pass
    except (OSError, ValueError):
        pass


def open_dashboard(vault: str) -> str | None:
    """Open the vault's dashboard in the browser.

    Starts `compartment dash` if this panel has not already started one.
    `dash` opens the browser itself the moment it is serving, so a first
    click needs only a started process; a later click reopens the URL it
    announced. Returns that URL, or None when the server did not start (a
    locked vault, for instance, which `dash` refuses).
    """
    proc, url = _DASH["proc"], _DASH["url"]
    if proc is not None and proc.poll() is None and url:
        webbrowser.open(url)
        return url
    try:
        proc = subprocess.Popen(
            [*_cli_argv(), "--vault", vault, "dash"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PATH": user_path()})
    except (OSError, subprocess.SubprocessError):
        return None
    _DASH["proc"], _DASH["url"] = proc, None
    url = None
    for line in proc.stdout:                # the announcement is line one
        url = dashboard_url(line)
        if url:
            break
    if not url:
        stop_dashboard()
        return None
    _DASH["url"] = url
    threading.Thread(target=_drain, args=(proc,), daemon=True).start()
    return url


def stop_dashboard() -> None:
    """End the dashboard this panel started, if it is still running."""
    proc = _DASH["proc"]
    _DASH["proc"], _DASH["url"] = None, None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def lock_vault(vault: str) -> bool:
    return _run([*_cli_argv(), "--vault", vault, "lock"])[0] == 0


def unlock_vault(vault: str, passphrase: str) -> tuple[bool, str]:
    """Unlock from the panel. Returns (ok, what to show the user).

    The passphrase goes down the child's stdin, never into argv: a command
    line is readable by every process on the machine. Argon2id deliberately
    takes its time, hence the generous timeout - a slow unlock is the point.
    """
    if not passphrase:
        return False, "enter your passphrase"
    try:
        p = subprocess.run(
            [*_cli_argv(), "--vault", vault, "unlock", "--passphrase-stdin"],
            input=passphrase + "\n", capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = " ".join(((p.stdout or "") + (p.stderr or "")).split())
    if p.returncode == 0:
        return True, "unlocked"
    low = out.lower()
    # Check this FIRST. If the CLI never ran, nothing in its output describes
    # the vault - and argparse prints "[--keyfile KEYFILE]" in its usage line,
    # which the keyfile test below happily mistook for a vault that needed a
    # 2FA keyfile it had never been given. Never diagnose the vault from a
    # message that is really about our own command line.
    if "unrecognized arguments" in low or low.startswith("usage:"):
        return False, "internal error: could not run the CLI"
    if "wrong passphrase" in low or "no keyslot" in low:
        return False, "wrong passphrase"
    if "keyfile" in low:
        return False, "needs its 2FA keyfile - plug it in and try again"
    return False, (out[:120] or "could not unlock")


def create_vault(vault: str, passphrase: str, repeat: str) -> tuple[bool, str]:
    """Create the vault from the panel. Returns (ok, what to show the user).

    The first-run step that was missing. Someone who installs the .pkg gets
    Compartment.app in /Applications and nothing at all on their PATH, so
    "no vault yet - run: compartment init" named a command their Terminal
    does not have; and the panel's only other control, Unlock, is hidden
    while there is no vault to unlock. Between the two, a fresh install had
    no reachable way to get a vault.

    `--no-app` because the app asking for this IS the app: without it `init`
    loads the login agent and starts a second copy, which then has to fight
    this one for the menu bar. Everything else `init` does on the way -
    seeding the starting memories, storing the session so the vault comes
    back unlocked, wiring the agents already installed here - is wanted.

    Both entries are compared here, where the user can see them, and the one
    they agreed on goes down the child's stdin: a command line is readable by
    every process on the machine.
    """
    if not passphrase:
        return False, "choose a passphrase"
    if passphrase != repeat:
        return False, "the two entries do not match"
    if os.path.exists(vault):
        return False, "there is already a vault here"
    try:
        p = subprocess.run(
            [*_cli_argv(), "--vault", vault, "init", "--passphrase-stdin",
             "--no-app"],
            input=passphrase + "\n", capture_output=True, text=True,
            timeout=900, encoding="utf-8", errors="replace",
            env={**os.environ, "PATH": user_path()})
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = " ".join(((p.stdout or "") + (p.stderr or "")).split())
    if p.returncode == 0:
        return True, "Vault created, and open. Nothing else to set up."
    low = out.lower()
    if "unrecognized arguments" in low or low.startswith("usage:"):
        return False, "internal error: could not run the CLI"
    if "already exists" in low:
        return False, "there is already a vault here"
    return False, (out[:160] or "could not create the vault")


def change_passphrase(vault: str, new: str, repeat: str) -> tuple[bool, str]:
    """Replace the passphrase from the panel. Returns (ok, what to show).

    The vault has to be open already - `rekey` re-wraps the master key, and
    the master key is only in hand while unlocked. So this asks for the new
    passphrase and not the old one: whoever is looking at an unlocked vault
    can already read every memory in it, which is strictly more than they
    gain by changing the credential.

    Both fields are compared here, where the user can see them, rather than
    making them type it twice into a terminal they cannot review.
    """
    if not new:
        return False, "enter a new passphrase"
    if new != repeat:
        return False, "the two entries do not match"
    try:
        p = subprocess.run(
            [*_cli_argv(), "--vault", vault, "rekey", "--new-passphrase-stdin"],
            input=new + "\n", capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = " ".join(((p.stdout or "") + (p.stderr or "")).split())
    if p.returncode == 0:
        return True, "passphrase changed"
    low = out.lower()
    if "unrecognized arguments" in low or low.startswith("usage:"):
        return False, "internal error: could not run the CLI"
    if "locked" in low:
        return False, "unlock the vault first"
    return False, (out[:120] or "could not change the passphrase")


#: The agents `compartment integrate` can wire, in the order they are offered.
#: Kept here rather than in the UI so the menu bar and the tray cannot drift
#: apart, and so adding a target is one edit.
INTEGRATION_TARGETS = (("claude", "Claude"),
                       ("hermes", "Hermes"),
                       ("openclaw", "OpenClaw"))


def integration_status(vault: str) -> dict:
    """Which agents are already wired to this machine.

    Reads each agent's own configuration, which is the only thing that can
    answer it: Compartment keeps no record of who it has been connected to,
    and a record it did keep could disagree with the truth after someone
    edited a config by hand. Best effort, and never raises - not being able
    to tell must not stop the button from working.

    Connected means an MCP server is registered, and nothing else counts.
    The capture hook used to be accepted here as a second-best answer, which
    made a machine with the hook and no server report itself connected while
    the model had no memory tools at all. An indicator that reports a state
    it has not checked is worse than no indicator.
    """
    out = {t: False for t, _ in INTEGRATION_TARGETS}
    try:                                                # Claude Code
        p = Path.home() / ".claude.json"
        if p.is_file():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            out["claude"] = "compartment" in (cfg.get("mcpServers") or {})
    except Exception:                                   # noqa: BLE001
        pass
    if not out["claude"]:
        try:                                            # Claude Desktop
            from . import claude_desktop
            out["claude"] = claude_desktop.is_registered()
        except Exception:                               # noqa: BLE001
            pass
    try:                                                # Hermes
        hermes = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        out["hermes"] = (hermes / "plugins" / "compartment"
                         / "plugin.yaml").is_file()
    except OSError:
        pass
    try:                                                # OpenClaw
        p = Path(os.environ.get("OPENCLAW_HOME",
                                Path.home() / ".openclaw")) / "openclaw.json"
        if p.is_file():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            out["openclaw"] = "compartment" in (cfg.get("mcpServers") or {})
    except (OSError, ValueError):
        pass
    return out


def agent_present(target: str, path: str | None = None) -> bool:
    """Whether this agent is installed on this machine at all.

    Kept separate from `integration_status` because the two questions have
    different answers and the panel needs both: `integrate hermes` writes the
    provider plugin whether or not Hermes is installed, so a config file we
    just wrote proves nothing about the program that is supposed to read it.

    Generous on purpose: a CLI on PATH, or the directory the agent keeps its
    own configuration in. Wiring an agent that is not installed writes a
    config nobody will ever read, and missing one that is installed leaves
    exactly the unpressed button the Connect row is here to avoid.

    `path` defaults to the PATH a terminal on this machine would have, which
    is not the one this process has: a login item inherits launchd's four
    system directories, and every agent CLI worth finding installs somewhere
    else.

    It lives here, in the module with no heavy imports, and not beside the
    rest of `integrate` in the CLI. The status bar app may import this one;
    importing the CLI for a shutil.which would cost it numpy for as long as
    it runs, and this is an app that is meant to be a quiet neighbour.

    Never raises. Not being able to tell must not stop the button working.
    """
    if path is None:
        path = user_path()
    try:
        from . import claude_desktop
        if target == "claude":
            return (bool(shutil.which("claude", path=path))
                    or claude_desktop.present())
        if target == "hermes":
            return (bool(shutil.which("hermes", path=path))
                    or Path(os.environ.get("HERMES_HOME",
                                           Path.home() / ".hermes")).is_dir())
        if target == "openclaw":
            return (bool(shutil.which("openclaw", path=path))
                    or Path(os.environ.get("OPENCLAW_HOME",
                                           Path.home() / ".openclaw")).is_dir())
    except Exception:                                   # noqa: BLE001
        return False
    return False


def connected_summary(vault: str) -> str:
    """One line naming the agents already wired, for the panel."""
    names = [n for t, n in INTEGRATION_TARGETS
             if integration_status(vault).get(t)]
    if not names:
        return "Not connected to an agent yet."
    return "Connected: " + ", ".join(names)


def integrate(vault: str, target: str) -> tuple[bool, str]:
    """Connect an agent to this vault, from the panel. Returns (ok, message).

    The same command the README gives, run for you. It is offered here
    because the terminal step after installing is where people stop: the
    vault exists, the icon is in the menu bar, and nothing is using it yet
    because nobody knew there was a second command.

    Wiring an agent edits that agent's own configuration, so this reports
    what happened rather than assuming - a Claude Code CLI that is not
    installed is a normal outcome, not an error to hide.

    What it reports is read back off the machine afterwards, never scraped
    out of the command's own prose. `integrate claude` wires two separate
    programs, and a Claude Desktop that was found, registered and backed up
    perfectly still leaves the line "Claude Code CLI not found" in the
    output. Searching that output for "not found" therefore answered a click
    with "Could not find Claude on this machine" on machines where Claude was
    installed, running, and by then connected - the one thing the button
    exists to tell the truth about.
    """
    if target not in dict(INTEGRATION_TARGETS):
        return False, f"unknown target {target!r}"
    name = dict(INTEGRATION_TARGETS)[target]
    present = agent_present(target)
    already = integration_status(vault).get(target, False)
    code, out = _run([*_cli_argv(), "--vault", vault, "integrate", target],
                     timeout=300)
    text = " ".join(out.split())
    if code != 0:
        return False, (text[:200] or f"could not connect {name}")
    # Asked before the wiring ran, because `integrate` writes some of the
    # configuration either way: after the fact there is no telling an agent
    # that is here from one whose config file we just created for it.
    if not present:
        return True, (f"Could not find {name} on this machine. Everything "
                      f"that does not need it is set up; install {name} and "
                      f"click again.")
    if not integration_status(vault).get(target, False):
        return False, (f"{name} is installed here, but the registration did "
                       f"not land. {text[-160:]}".strip())
    # Wiring is idempotent, so the button always runs it: someone clicking it
    # a second time usually has a reason, like a vault that has moved. What
    # changes is what it says afterwards, because "connected" in answer to a
    # click that changed nothing reads as a lie.
    if already:
        return True, (f"{name} was already connected. Checked again and left "
                      f"it wired to this vault.")
    return True, f"{name} is connected. Restart {name} to pick up the change."


def summarise(state: dict) -> str:
    """One line under the title. Also what --self-check prints."""
    if not state["exists"]:
        return "no vault"
    if state["locked"]:
        return "locked - enter your passphrase to unlock"
    return (f"{state['records']:,} memories · "
            f"{state['organic']:,} stored by you")


def starter_note(state: dict) -> str:
    """What the recent list says when nothing has been stored during use.

    A new vault holds thousands of starting memories and no organic ones, so
    the recent feed is legitimately empty on day one. "Nothing yet" there
    reads as an empty vault, which is the opposite of what happened."""
    seeded = max(int(state.get("records", 0)) - int(state.get("organic", 0)), 0)
    if not seeded:
        return "nothing stored yet"
    return (f"nothing stored during use yet - {seeded:,} starting memories "
            "came with the vault and are searchable now")


def auto_lock_label(minutes: int) -> str:
    return "Never" if not minutes else f"{minutes} min"


# ---------------------------------------------------------------- login item

LOGIN_STATUS = {0: "not registered", 1: "enabled", 2: "requires approval",
                3: "not found"}


def _app_service():
    """SMAppService for this bundle, or None when we are not running from
    Compartment.app (a loose `compartment menubar` has no bundle to register)."""
    try:
        import objc
        from Foundation import NSBundle
    except ImportError:
        return None
    # It has to be OUR bundle, not merely SOME bundle. A framework Python
    # reports bundleIdentifier "org.python.python" with a bundle path inside
    # Python.framework, so a bare "is there an identifier" test passes on an
    # ordinary pip install. SMAppService would then act on Python.app: status
    # comes back "not found", and registering would put PYTHON in the user's
    # login items instead of Compartment.
    if (NSBundle.mainBundle().bundleIdentifier() or "") != BUNDLE_ID:
        return None
    try:
        objc.loadBundle(
            "ServiceManagement", globals(),
            bundle_path="/System/Library/Frameworks/ServiceManagement.framework")
        return objc.lookUpClass("SMAppService").mainAppService()
    except Exception:                                   # noqa: BLE001
        return None


# A pip install has no app bundle, and SMAppService can only register one. So
# the bundle path is used when there is a bundle, and a LaunchAgent otherwise.
# Without this, `pip install compartment` could never start at login on macOS,
# which would make the status bar app a second-class citizen of the very
# install most people use.
# Must match tools/build_macos_app.py: the identity of the real app bundle.
BUNDLE_ID = "io.github.maxfreedompollard.compartment"
LAUNCH_AGENT_LABEL = f"{BUNDLE_ID}.menubar"


def _agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


#: Where a pip install keeps the small bundle it needs to have a name and an
#: icon. Never /Applications: that belongs to the .pkg, and writing there
#: needs a password nobody should be asked for by `pip install`.
USER_APP_BUNDLE = Path.home() / "Applications" / "Compartment.app"


def installed_app_bundle() -> Path | None:
    """The real bundle from the .pkg or the .dmg, if this Mac has one."""
    for p in (Path("/Applications/Compartment.app"),
              Path.home() / "Applications" / "Compartment.app"):
        if (p / "Contents" / "MacOS").is_dir() and not _is_generated(p):
            return p
    return None


#: In Resources, not in Contents. codesign seals a bundle by walking it, and
#: an unexpected file directly under Contents makes it refuse - which left
#: the bundle unsigned and macOS still calling it an unidentified developer.
GENERATED_MARKER = ("Contents", "Resources", ".generated")


def _is_generated(bundle: Path) -> bool:
    return bundle.joinpath(*GENERATED_MARKER).is_file()


def ensure_login_bundle() -> Path | None:
    """Give a pip install something macOS can name and draw.

    A LaunchAgent that runs a bare executable is listed in Login Items as a
    blank page with no icon, and under App Background Activity as an
    anonymous "compartment". macOS takes the name and the icon from an
    application bundle, and a pip install has none - so it gets a small one,
    which does nothing but start the same CLI.

    Returns the bundle to launch, or None if a real one is already installed
    or the bundle could not be written.
    """
    if sys.platform != "darwin":
        return None
    real = installed_app_bundle()
    if real is not None:
        return real
    icns = Path(__file__).resolve().parent / "data" / "app.icns"
    exe = compartment_bin()
    try:
        macos = USER_APP_BUNDLE / "Contents" / "MacOS"
        res = USER_APP_BUNDLE / "Contents" / "Resources"
        macos.mkdir(parents=True, exist_ok=True)
        res.mkdir(parents=True, exist_ok=True)
        (USER_APP_BUNDLE / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            '  <key>CFBundleName</key><string>Compartment</string>\n'
            '  <key>CFBundleDisplayName</key><string>Compartment</string>\n'
            '  <key>CFBundleExecutable</key><string>Compartment</string>\n'
            f'  <key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>\n'
            '  <key>CFBundleIconFile</key><string>app</string>\n'
            '  <key>CFBundlePackageType</key><string>APPL</string>\n'
            f'  <key>CFBundleShortVersionString</key><string>{__version__}'
            '</string>\n'
            '  <key>LSUIElement</key><true/>\n'
            '</dict>\n</plist>\n', encoding="utf-8")
        launcher = macos / "Compartment"
        launcher.write_text(
            "#!/bin/sh\n"
            "# Generated by `compartment menubar --login on`. It exists so\n"
            "# macOS has a bundle to take a name and an icon from.\n"
            f'exec "{exe}" menubar "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        if icns.is_file():
            shutil.copyfile(icns, res / "app.icns")
        USER_APP_BUNDLE.joinpath(*GENERATED_MARKER).write_text(
            "written by compartment; safe to delete\n", encoding="utf-8")
    except OSError:
        return None
    # Ad-hoc signing is what turns "Item from unidentified developer" into
    # the app's own name. Best effort: a Mac without the command line tools
    # still gets the icon.
    try:
        subprocess.run(["codesign", "--force", "--sign", "-",
                        str(USER_APP_BUNDLE)], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    return USER_APP_BUNDLE


def _launcher_argv() -> list[str]:
    """How to start the status bar app again later, from wherever we live."""
    bundle = ensure_login_bundle()
    if bundle is not None:
        exe = bundle / "Contents" / "MacOS" / "Compartment"
        if exe.is_file():
            return [str(exe)]
    exe = shutil.which("compartment")
    if exe:
        return [exe, "menubar"]
    return [sys.executable, "-m", "compartment.cli", "menubar"]


def _set_login_agent(enabled: bool, vault: str | None = None) -> str:
    plist = _agent_plist()
    if not enabled:
        # bootout first: with KeepAlive set, a running copy that is still
        # registered would be restarted by launchd after the plist was
        # deleted, and the icon would come back from a file that no longer
        # exists. Unload stays as the fallback for older macOS.
        code, msg = _launchctl("bootout", f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}")
        _launchctl("unload", str(plist))
        plist.unlink(missing_ok=True)
        # Deleting the file is not what stops it. launchd holds the job
        # definition in memory once bootstrapped, so a bootout that failed
        # leaves KeepAlive free to bring the icon back from a plist that no
        # longer exists - after the user asked for it to be off. 3 is
        # launchd's "no such process", which is the state we wanted anyway.
        if code not in (0, 3) and _agent_loaded():
            return f"failed: launchd still has the agent registered: {msg}"
        return "off"
    argv = _launcher_argv()
    args = "".join(f"        <string>{_plist_text(a)}</string>\n"
                   for a in argv)
    # Which vault to open, carried in the environment rather than on the
    # command line. The agent runs an app bundle, and the launcher inside a
    # .pkg bundle is not ours to change, so there is nowhere to put a
    # --vault: it has to go before the subcommand and the launcher appends.
    # Without this the login item quietly opened the DEFAULT vault, however
    # the user had registered it - the Windows and Linux entries have always
    # carried the path, and only macOS dropped it.
    vault = vault or default_vault()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '  <dict>\n'
        f'    <key>Label</key><string>{LAUNCH_AGENT_LABEL}</string>\n'
        '    <key>ProgramArguments</key>\n'
        f'    <array>\n{args}    </array>\n'
        '    <key>RunAtLoad</key><true/>\n'
        # KeepAlive was <false/>, which made this a one-shot: launchd started
        # the app at login and then never looked at it again. Anything that
        # ended the process - a crash, an ObjC exception, macOS reclaiming
        # memory, an upgrade replacing the binary underneath it - took the
        # icon out of the menu bar until the next login, with nothing said.
        # A status bar app is meant to sit there for as long as the machine
        # is up.
        #
        # SuccessfulExit false is the form that means "bring it back if it
        # died, leave it alone if it left". Quit calls NSApp.terminate_, which
        # exits 0, so the one way to remove the icon is still the one the user
        # asked for. A crash exits non-zero and comes straight back.
        '    <key>KeepAlive</key>\n'
        '    <dict>\n'
        '      <key>SuccessfulExit</key><false/>\n'
        '    </dict>\n'
        '    <key>EnvironmentVariables</key>\n'
        '    <dict>\n'
        '      <key>COMPARTMENT_VAULT</key>\n'
        f'      <string>{_plist_text(vault)}</string>\n'
        '    </dict>\n'
        '  </dict>\n'
        '</plist>\n', encoding="utf-8")
    return _bootstrap_agent(plist)


def _plist_text(value: str) -> str:
    """A path is XML here, and a vault living under a folder with an
    ampersand in its name would otherwise write a plist launchd cannot
    parse - which fails at the next login, not at the moment it is set."""
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _launchctl(*argv: str) -> tuple[int, str]:
    """Run launchctl and actually look at what it said."""
    try:
        r = subprocess.run(["launchctl", *argv], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return r.returncode, (r.stderr or r.stdout or "").strip()


def _gui_domain() -> str:
    """launchd's per-user domain. `os.getuid` is POSIX-only and this module
    is imported on every platform, so it is asked for rather than assumed:
    an AttributeError here would take down anything that merely touched the
    login-agent code on Windows."""
    uid = getattr(os, "getuid", None)
    return f"gui/{uid() if uid else 0}"


def _agent_loaded() -> bool:
    """Does launchd have this agent right now?

    The one question that matters, asked of the only thing that can answer
    it. A plist in ~/Library/LaunchAgents is a request, not a registration:
    a bootstrap that was refused leaves the file behind exactly as it leaves
    the login empty, and nothing on disk tells the two apart.
    """
    code, _ = _launchctl("print", f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}")
    return code == 0


def _is_launchd_managed() -> bool:
    """Was this copy started by the login agent?

    launchd puts the job label in XPC_SERVICE_NAME for the processes it
    spawns. A copy started from a shell has "0" there and a copy started
    from Finder has the app's own service name, so this is true only of the
    one copy launchd is watching - and being watched is the whole of what
    entitles a copy to take the menu bar from another.
    """
    return os.environ.get("XPC_SERVICE_NAME", "") == LAUNCH_AGENT_LABEL


def _bootstrap_agent(plist: Path) -> str:
    """Load the agent, and report a refusal as one.

    The old code ran `launchctl load` and threw the result away, so a plist
    that was written but never loaded reported "on" and started nothing at
    the next login - which is exactly the state a machine ends up in when
    the label is already bootstrapped and load quietly refuses.

    `bootstrap` is the modern verb and the only one that reports properly.
    An existing registration is booted out first so this is idempotent, and
    `load` remains as the fallback for macOS versions that predate it.

    Neither verb is taken at its word at the end. `load` in particular
    returns zero for jobs it declined, and the exit status of a command is a
    weaker claim than launchd's own answer to "do you have this?".
    """
    domain = _gui_domain()
    _launchctl("bootout", f"{domain}/{LAUNCH_AGENT_LABEL}")   # ok if absent
    code, msg = _launchctl("bootstrap", domain, str(plist))
    if code != 0:
        code, msg2 = _launchctl("load", "-w", str(plist))     # older macOS
        msg = msg or msg2
    if _agent_loaded():
        return "on"
    return f"failed: {msg or 'launchctl refused the agent'}"


def login_status() -> str:
    svc = _app_service()
    if svc is not None:
        return LOGIN_STATUS.get(svc.status(), str(svc.status()))
    # No bundle to register, so the LaunchAgent is what starts this at login
    # - and whether it does is launchd's to answer, not the filesystem's.
    # This used to be `"on" if the plist exists`, which is the same mistake
    # `_bootstrap_agent` was written to stop making on the way in: a plist
    # written by a bootstrap that failed reported start-at-login as working,
    # every login, for ever, on the pip install that every user without the
    # .pkg has.
    if not _agent_plist().is_file():
        return "off"
    return "on" if _agent_loaded() else "off (the agent is not loaded)"


def set_login(enabled: bool, vault: str | None = None) -> str:
    """Start at login, via the modern API so System Settings lists Compartment by
    name with its icon instead of an anonymous "legacy agent" entry."""
    svc = _app_service()
    if svc is None:
        # pip install: no bundle to register
        return _set_login_agent(enabled, vault)
    try:
        res = (svc.registerAndReturnError_(None) if enabled
               else svc.unregisterAndReturnError_(None))
    except Exception as exc:                            # noqa: BLE001
        return f"failed: {exc}"
    # PyObjC turns the NSError** out-parameter into a second return value.
    # The old code dropped it, so a refusal - "Could not connect to system
    # service", which is what registering from a root installer script gets
    # you - was reported as success.
    ok, err = res if isinstance(res, tuple) else (bool(res), None)
    if not ok:
        detail = ""
        try:
            detail = str(err.localizedDescription()) if err is not None else ""
        except Exception:                               # noqa: BLE001
            pass
        return f"failed: {detail or 'the login item was refused'}"
    return login_status()


# --------------------------------------------------- outliving the terminal

#: Set on the copy a detached relaunch starts, so it never detaches twice.
DETACHED_ENV = "COMPARTMENT_DETACHED"

#: Set by hand to keep the app in the foreground, for debugging.
FOREGROUND_ENV = "COMPARTMENT_FOREGROUND"


def started_from_a_terminal() -> bool:
    """Is this copy tied to a terminal that can take it down?

    `compartment menubar` typed at a prompt is a child of that shell, in the
    foreground process group of its tty. Closing the window sends SIGHUP to
    that whole group, and the default disposition for SIGHUP is to die - so
    the icon goes when the window goes, which is not what a status bar app
    is for.
    """
    if os.environ.get(DETACHED_ENV) or os.environ.get(FOREGROUND_ENV):
        return False
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.isatty():
                return True
        except (OSError, ValueError):
            continue
    return False


def relaunch_detached(vault: str, show: bool = False,
                      timeout: float = 20.0) -> bool:
    """Start the app again in a session of its own, and hand over to it.

    `start_new_session` makes the new copy a session leader with no
    controlling terminal, so nothing about the window it was typed in can
    reach it: no SIGHUP when the window closes, no death when the shell
    exits. Windows has no sessions in that sense and uses DETACHED_PROCESS,
    which cuts the same tie to the console.

    Returns False, holding the lock, unless the new copy really came up.
    """
    # Through the console script, not `python -m`, so the new copy keeps the
    # name the old one had. Windows identifies this app by its image name
    # everywhere it matters - Get-Process, tasklist, taskkill /IM - so a
    # relaunch as python.exe is a copy that is running and cannot be found,
    # stopped, or counted by anything that goes looking for it.
    exe = compartment_bin()
    argv = ([exe, "--vault", vault, "menubar"] if Path(exe).is_file()
            else _cli_argv() + ["--vault", vault, "menubar"])
    if show:
        argv.append("--show")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL,
              "env": {**os.environ, DETACHED_ENV: "1"}}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008          # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    before = set(running_pids())
    release_instance_lock()
    try:
        subprocess.Popen(argv, **kwargs)              # noqa: S603
    except (OSError, subprocess.SubprocessError):
        acquire_instance_lock(vault)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if set(running_pids()) - before:
            return True
        time.sleep(0.2)
    _handle, only = acquire_instance_lock(vault)
    return not only


def running_pids() -> list[int]:
    """Every other status bar copy on this machine."""
    try:
        out = subprocess.run(["pgrep", "-f", "compartment.*menubar"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    me = os.getpid()
    return [int(x) for x in out.split() if x.isdigit() and int(x) != me]


def quit_running(timeout: float = 10.0) -> bool:
    """Stop a running status bar app, so an update or uninstall does not leave
    the old build sitting in the menu bar with its binary already gone.

    Waits for the process to actually go. Sending a signal is not the same
    as being obeyed, and the caller's next move - starting the new build -
    is the one thing that must not happen while the old one still holds the
    lock, because the new copy would stand down and the user would be told
    the app had restarted while looking at the build it replaced.
    """
    import signal
    pids = running_pids()
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not running_pids():
            return True
        time.sleep(0.1)
    return not running_pids()


def restart_agent() -> bool:
    """Stop and start the login agent's copy in one launchd operation.

    What an upgrade needs. A plain SIGTERM races: KeepAlive relaunches a
    process that exited non-zero, so the old copy comes back on its own
    while the caller is starting a replacement, and the two fight over the
    lock. `kickstart -k` is launchd doing both halves itself, and because
    the job is unchanged the new process picks up whatever the upgrade put
    at the path the plist names.
    """
    if sys.platform != "darwin" or not _agent_loaded():
        return False
    code, _ = _launchctl("kickstart", "-k",
                         f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}")
    return code == 0


def evict_unsupervised(vault: str, timeout: float = 10.0) -> bool:
    """Take this vault's menu bar from a copy that nothing is supervising.

    Only the launchd copy is entitled to call this, and only the one process
    named in the lock file is touched - see `lock_holder_pid`. An incumbent
    that never wrote its pid is left alone: standing down costs an icon
    until the next login, and killing the wrong process costs more.
    """
    import signal
    pid = lock_holder_pid(vault)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def hand_over_to_login_agent(vault: str, timeout: float = 15.0) -> bool:
    """Let the supervised copy have the menu bar, and prove that it took it.

    The other half of the same precedence rule. A copy started by hand from
    a terminal is a child of that terminal: closing the window SIGHUPs the
    foreground process group and the icon goes with it, and nothing is
    watching to put it back. Worse, while that copy holds the lock, the copy
    launchd starts stands down with exit 0 - which `KeepAlive
    SuccessfulExit=false` reads as "it left on purpose" - so launchd never
    tries again either, and the menu bar belongs to the one copy that cannot
    survive the session.

    So a loose copy asks launchd to run the real one instead. A plist that
    launchd has never heard of is bootstrapped on the way past, which is the
    repair for a machine left with a written-but-never-loaded agent. A
    missing plist is left alone: that means either a machine that never had
    start-at-login or a user who turned it off, and neither is ours to
    overrule.

    Returns False - and keeps the lock - unless a new copy really came up.
    An icon nothing supervises still beats no icon at all.
    """
    if sys.platform != "darwin" or _is_launchd_managed():
        return False
    plist = _agent_plist()
    if not plist.is_file():
        return False
    # Both of these happen before anything is asked to start. The agent has
    # RunAtLoad, so bootstrapping it starts a copy straight away: hold the
    # lock a moment longer and that copy stands down with exit 0, which is
    # the one exit KeepAlive will not relaunch - the handover would defeat
    # itself. And a pid taken afterwards would already include it.
    before = set(running_pids())
    release_instance_lock()
    if not _agent_loaded() and _bootstrap_agent(plist) != "on":
        acquire_instance_lock(vault)
        return False
    code, _ = _launchctl("kickstart", f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}")
    if code == 0:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if set(running_pids()) - before:
                return True
            time.sleep(0.2)
    # Nothing came up, so take the menu bar back rather than leave it empty.
    # Unless the lock has gone to somebody in the meantime, which means the
    # handover happened after all, just slower than we waited.
    _handle, only = acquire_instance_lock(vault)
    return not only


# ----------------------------------------------------------------- self test

def self_check(vault: str | None = None) -> int:
    """Prove the data layer works without opening a window (CI + debugging)."""
    v = vault or default_vault()
    st = fetch_state(v)
    print(f"vault    : {st['vault']}")
    print(f"status   : {summarise(st)}")
    if st["error"]:
        print(f"note     : {st['error']}")
    s = st["settings"]
    print(f"settings : capture_hook={s['capture_hook']} "
          f"search_starter_facts={s['search_starter_facts']} "
          f"auto_lock={auto_lock_label(s['auto_lock_minutes'])}")
    print(f"recent   : {len(st['recent'])} memories")
    for r in st["recent"]:
        text = " ".join((r.get("text") or "").split())
        print(f"  [{r.get('created_local','')}] {text[:88]}")
    return 0


# ------------------------------------------------------------------- the UI

def run(vault: str | None = None, show: bool = False,
        render_to: str | None = None) -> int:
    """Start the status bar app. Returns only when the user quits.

    `render_to` writes the popover to a PNG and exits instead of running -
    a way to look at the UI without screen-recording permission. Run it with
    a normal (framework) Python during development: snapshotting from the
    interpreter embedded in Compartment.app draws the controls but not the text,
    an artefact of the offscreen text system there. The live popover is
    unaffected, because app.run() performs a full launch.
    """
    if sys.platform != "darwin":
        print("error: the menu bar app is macOS only", file=sys.stderr)
        return 1
    try:
        import objc                                     # noqa: F401
        from AppKit import (NSApp, NSApplication,
                            NSApplicationActivationPolicyAccessory,
                            NSBackingStoreBuffered, NSBox, NSColor,
                            NSRunningApplication,
                            NSFont, NSImage, NSMakeRect, NSMenu, NSMenuItem,
                            NSPanel, NSPopover, NSScreen, NSSegmentedControl,
                            NSClipView, NSScrollView,
                            NSBitmapImageRep, NSGraphicsContext,
                            NSSecureTextField, NSStackView, NSStatusBar,
                            NSSwitch, NSTextField,
                            NSButton, NSView, NSViewController,
                            NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
                            NSWindowStyleMaskUtilityWindow)
        from Foundation import (NSBundle, NSDistributedNotificationCenter,
                                NSObject)
    except ImportError:
        print("error: the menu bar app needs PyObjC.\n"
              "  pip install 'compartment[menubar]'", file=sys.stderr)
        return 1

    vault_path = vault or default_vault()

    # One running copy, one icon. LaunchServices normally turns a second
    # launch into a reopen event for the copy already running, but a copy
    # started straight from the executable is invisible to it - and then
    # opening Compartment.app quietly adds a second status item next to the first
    # instead of showing the panel. Handing off over a distributed
    # notification works whichever way each copy was started.
    if not render_to and (NSBundle.mainBundle().bundleIdentifier() or ""):
        bundle_id = NSBundle.mainBundle().bundleIdentifier()
        mine = NSRunningApplication.currentApplication().processIdentifier()
        peers = [a for a in NSRunningApplication
                 .runningApplicationsWithBundleIdentifier_(bundle_id)
                 if a.processIdentifier() != mine]
        if peers:
            NSDistributedNotificationCenter.defaultCenter(
            ).postNotificationName_object_userInfo_deliverImmediately_(
                SHOW_NOTIFICATION, None, None, True)
            print("Compartment is already running - asked it to show its panel")
            return 0

    # The check above can only see bundled applications, and a pip install is
    # a bare Python process with no bundle identifier at all - so it saw
    # nothing, every launch believed it was the first, and `init` (which
    # loads a RunAtLoad LaunchAgent and then starts the app itself) put two
    # icons in the menu bar of every pip install. The lock does not care how
    # a copy was started.
    if not render_to:
        _lock, only = acquire_instance_lock(vault_path)
        # Precedence between two copies of the same app: the one launchd is
        # supervising wins, whichever started first. Held from both ends -
        # the managed copy takes the menu bar from an unsupervised incumbent
        # here, and an unsupervised copy hands it over below.
        if not only and _is_launchd_managed() and evict_unsupervised(vault_path):
            _lock, only = acquire_instance_lock(vault_path)
        if not only:
            NSDistributedNotificationCenter.defaultCenter(
            ).postNotificationName_object_userInfo_deliverImmediately_(
                SHOW_NOTIFICATION, None, None, True)
            print("Compartment is already running - asked it to show its panel")
            return 0
        if hand_over_to_login_agent(vault_path):
            print("Compartment starts at login on this Mac, so that copy has "
                  "the menu bar - launchd puts it back if it ever dies.")
            return 0
        # No login agent to hand to, so at least cut the tie to the terminal
        # this was typed in. Otherwise closing that window takes the icon.
        if started_from_a_terminal() and relaunch_detached(vault_path, show):
            print("Compartment is in your menu bar. It is running on its own "
                  "now, so closing this terminal will not stop it.\n"
                  "  To have it come back at every login: compartment "
                  "menubar --login on")
            return 0

    # -- small helpers on top of AppKit's verbosity ------------------------
    def label(text, size=13, bold=False, secondary=False, wrap=False,
              truncate=False, width=CONTENT_WIDTH):
        f = NSTextField.labelWithString_(text)
        f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
        if secondary:
            f.setTextColor_(NSColor.secondaryLabelColor())
        f.setAlignment_(0)                  # NSTextAlignmentLeft
        if wrap:
            f.setLineBreakMode_(0)          # NSLineBreakByWordWrapping
            f.setUsesSingleLineMode_(False)
            f.setPreferredMaxLayoutWidth_(width)
            f.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        elif truncate:
            # one line, ellipsised - a long tag list must not blow the width
            f.setLineBreakMode_(4)          # NSLineBreakByTruncatingTail
            f.widthAnchor().constraintLessThanOrEqualToConstant_(
                width).setActive_(True)
        return f

    def row(*views, spacing=8):
        s = NSStackView.stackViewWithViews_(list(views))
        s.setOrientation_(_HORIZONTAL)
        s.setSpacing_(spacing)
        s.setAlignment_(_CENTER_Y)
        # fill the popover width, otherwise the trailing control floats
        s.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH).setActive_(True)
        return s

    def divider():
        # NSBox's own separator, not a layer-backed view: reaching through to
        # CGColor logs an ObjCPointerWarning on every single rebuild.
        b = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_WIDTH, 1))
        b.setBoxType_(2)                                # NSBoxSeparator
        b.widthAnchor().constraintEqualToConstant_(
            CONTENT_WIDTH).setActive_(True)
        return b

    class TopClip(NSClipView):
        """A clip view whose origin is its top left corner.

        AppKit's default is the bottom left, which inside a scroll view means
        the panel opens showing its last line, with the title somewhere above
        the fold. Flipping it makes "scrolled to the top" the resting state.
        """

        def isFlipped(self):
            return True

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.state = fetch_state(vault_path)
            self.popover = None
            self.status_item = None
            self.body = None
            self.window = None
            self.pw_field = None      # only while the vault is locked
            self.unlock_note = None   # "wrong passphrase", and the like
            self.changing_pw = False  # the change-passphrase fields are open
            self.pw_new = None
            self.pw_repeat = None
            self.change_note = None
            self.connect_note = None  # what the last Connect button reported
            self.connect_busy = None  # the agent being wired right now
            self.new_pw = None        # only while there is no vault yet
            self.new_repeat = None
            self.create_note = None   # "the two entries do not match", etc
            self.creating = False     # the vault is being made right now
            return self

        # ---- building the popover contents -----------------------------
        @objc.python_method
        def _finish(self, views):
            stack = NSStackView.stackViewWithViews_(views)
            stack.setOrientation_(_VERTICAL)
            stack.setAlignment_(_LEADING)
            stack.setSpacing_(6)
            stack.setEdgeInsets_((CONTENT_INSET, CONTENT_INSET,
                                  CONTENT_INSET, CONTENT_INSET))
            stack.layoutSubtreeIfNeeded()
            fit = stack.fittingSize()
            stack.setFrameSize_((POPOVER_WIDTH, fit.height))
            if fit.height <= POPOVER_MAX_HEIGHT:
                return stack

            # Taller than a popover is allowed to be. Scroll it. What this
            # did before was cut the overflow off and say nothing, so the
            # buttons below the fold were not merely out of reach - there
            # was no sign they existed. That is how the passphrase form
            # came to show one box out of two.
            stack.setTranslatesAutoresizingMaskIntoConstraints_(True)
            box = NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT)
            scroll = NSScrollView.alloc().initWithFrame_(box)
            scroll.setContentView_(TopClip.alloc().initWithFrame_(box))
            scroll.setHasVerticalScroller_(True)
            scroll.setDrawsBackground_(False)
            scroll.setDocumentView_(stack)
            return scroll

        @objc.python_method
        def buildChangeBody(self, st):
            """Changing the passphrase gets the whole panel to itself.

            The ordinary panel already fills POPOVER_MAX_HEIGHT, and this
            popover clips instead of scrolling. Appending the fields to the
            bottom of it put the second box, Save, and the error note below
            the visible edge: you saw one box, pressed Return, and the save
            failed its repeat check against an empty field you could neither
            fill nor read the complaint about. A focused view cannot overflow.
            """
            def secure(placeholder):
                f = NSSecureTextField.alloc().initWithFrame_(
                    NSMakeRect(0, 0, CONTENT_WIDTH, 24))
                f.setPlaceholderString_(placeholder)
                f.setTarget_(self)
                f.setAction_("saveChangePw:")     # Return saves
                return f

            self.pw_new = secure("New passphrase")
            self.pw_repeat = secure("Repeat it")
            views = [row(label("Change password", 15, bold=True), _spacer()),
                     label(summarise(st), 11, secondary=True),
                     divider(),
                     row(self.pw_new),
                     row(self.pw_repeat)]
            if self.change_note:
                views.append(label(self.change_note, 11, secondary=True,
                                   wrap=True))
            views.append(label("Both boxes must match. There is no recovery "
                               "phrase - if you forget this, the memories are "
                               "unrecoverable.", 10, secondary=True, wrap=True))
            save_b = NSButton.buttonWithTitle_target_action_("Save", self,
                                                             "saveChangePw:")
            save_b.setKeyEquivalent_("\r")
            cancel_b = NSButton.buttonWithTitle_target_action_(
                "Cancel", self, "cancelChangePw:")
            views.append(row(save_b, cancel_b, _spacer()))
            return self._finish(views)

        def buildBody(self):
            st = self.state
            if self.changing_pw and st["exists"] and not st["locked"]:
                return self.buildChangeBody(st)
            views = []
            # "locked" over an empty disk describes a vault that is not
            # there, and sends the user looking for the Unlock control that
            # sentence implies.
            if not st["exists"]:
                badge = "not set up"
            else:
                badge = "locked" if st["locked"] else "unlocked"
            title = row(label("Compartment", 15, bold=True),
                        label(badge, 11, secondary=True))
            views.append(title)
            views.append(label(summarise(st), 11, secondary=True))
            if st["error"]:
                views.append(label(st["error"], 11, secondary=True, wrap=True))

            # Making the vault, and then opening it, belong here. The vault
            # is the product, and neither should mean finding a terminal -
            # least of all on an install that never put a `compartment`
            # command in one.
            self.pw_field = None
            self.new_pw = self.new_repeat = None
            if not st["exists"]:
                def newfield(placeholder):
                    f = NSSecureTextField.alloc().initWithFrame_(
                        NSMakeRect(0, 0, CONTENT_WIDTH, 24))
                    f.setPlaceholderString_(placeholder)
                    f.setTarget_(self)
                    f.setAction_("createVault:")      # Return creates too
                    return f

                self.new_pw = newfield("Choose a passphrase")
                self.new_repeat = newfield("Repeat it")
                views.append(row(self.new_pw))
                views.append(row(self.new_repeat))
                create_b = NSButton.buttonWithTitle_target_action_(
                    "Create vault", self, "createVault:")
                create_b.setKeyEquivalent_("\r")
                create_b.setEnabled_(not self.creating)
                views.append(row(create_b, _spacer()))
                if self.create_note:
                    views.append(label(self.create_note, 11, bold=True,
                                       wrap=True))
                views.append(label("There is no recovery phrase - if you "
                                   "forget this, the memories are "
                                   "unrecoverable.", 10, secondary=True,
                                   wrap=True))
            elif st["locked"]:
                field = NSSecureTextField.alloc().initWithFrame_(
                    NSMakeRect(0, 0, CONTENT_WIDTH - 92, 24))
                field.setPlaceholderString_("Passphrase")
                field.setTarget_(self)
                field.setAction_("unlockNow:")        # Return unlocks too
                self.pw_field = field
                unlock_b = NSButton.buttonWithTitle_target_action_(
                    "Unlock", self, "unlockNow:")
                unlock_b.setKeyEquivalent_("\r")
                views.append(row(field, unlock_b))
                if self.unlock_note:
                    views.append(label(self.unlock_note, 11, secondary=True,
                                       wrap=True))
                views.append(label("Stays unlocked until restart or Lock",
                                   10, secondary=True))

            # Changing the passphrase is a separate view: see buildChangeBody.
            self.pw_new = self.pw_repeat = None
            if self.change_note:
                views.append(label(self.change_note, 11, secondary=True,
                                   wrap=True))
            views.append(divider())

            views.append(label("SETTINGS", 10, bold=True, secondary=True))
            s = st["settings"]

            self.hook_switch = NSSwitch.alloc().init()
            self.hook_switch.setState_(1 if s["capture_hook"] else 0)
            self.hook_switch.setTarget_(self)
            self.hook_switch.setAction_("toggleHook:")
            # No explanatory line under these two: the switch says what it
            # does, and a popover cannot grow past the height macOS gives it
            # without putting the whole panel behind a scrollbar.
            views.append(row(label("Create memories automatically"), _spacer(),
                             self.hook_switch))

            self.starter_switch = NSSwitch.alloc().init()
            self.starter_switch.setState_(1 if s["search_starter_facts"] else 0)
            self.starter_switch.setTarget_(self)
            self.starter_switch.setAction_("toggleStarter:")
            views.append(row(label("Search starter facts"), _spacer(),
                             self.starter_switch))

            # A memory stored with a last day on it is removed once that day
            # has gone. Off, the date is recorded and shown and nothing is
            # ever deleted. Nothing without an expiry is touched either way.
            self.expire_switch = NSSwitch.alloc().init()
            self.expire_switch.setState_(1 if s["expire_memories"] else 0)
            self.expire_switch.setTarget_(self)
            self.expire_switch.setAction_("toggleExpire:")
            views.append(row(label("Forget memories when they expire"),
                             _spacer(), self.expire_switch))

            seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
                [auto_lock_label(m) for m in AUTO_LOCK_CHOICES], 0, self,
                "changeAutoLock:")
            try:
                seg.setSelectedSegment_(
                    AUTO_LOCK_CHOICES.index(int(s["auto_lock_minutes"])))
            except ValueError:
                seg.setSelectedSegment_(1)
            self.autolock_seg = seg
            views.append(row(label("Auto-lock"), _spacer(), seg))
            views.append(label("Lock the vault after this much idle time",
                               10, secondary=True))
            views.append(divider())

            # Installing leaves you with a vault and an icon, and nothing
            # using either until an agent is wired to it. That step is one
            # terminal command, which is one command too many for most
            # people, so it is a button.
            views.append(label("CONNECT AN AGENT", 10, bold=True,
                               secondary=True))
            wired = integration_status(vault_path)
            connect_buttons = []
            for i, (target, name) in enumerate(INTEGRATION_TARGETS):
                b = NSButton.buttonWithTitle_target_action_(
                    f"{name} ✓" if wired.get(target) else name,
                    self, "connectAgent:")
                b.setTag_(i)
                b.setEnabled_(self.connect_busy is None)
                connect_buttons.append(b)
            views.append(row(*connect_buttons, _spacer()))
            # Only ever the result of a click. The heading and the buttons
            # already say what this does, and a standing explanation here
            # cost three lines the popover does not have: past the height
            # macOS allows, the whole panel goes behind a scrollbar.
            if self.connect_note:
                views.append(label(self.connect_note, 11, bold=True,
                                   wrap=True))
            views.append(divider())

            head = label(f"LAST {RECENT_COUNT} MEMORIES", 10, bold=True,
                         secondary=True)
            if st["exists"] and not st["locked"]:
                # The five here are a glance; the whole vault is a page.
                dash_b = NSButton.buttonWithTitle_target_action_(
                    "Dashboard", self, "openDashboard:")
                dash_b.setToolTip_("Open the whole vault in your browser: "
                                   "growth, the relation graph, tags, search")
                views.append(row(head, _spacer(), dash_b))
            else:
                views.append(head)
            if not st["exists"]:
                views.append(label("nothing yet - create the vault above", 11,
                                   secondary=True))
            elif st["locked"]:
                views.append(label("unlock the vault to see them", 11,
                                   secondary=True))
            elif not st["recent"]:
                views.append(label(starter_note(st), 11, secondary=True,
                                   wrap=True))
            else:
                for r in st["recent"]:
                    text = " ".join((r.get("text") or "").split())
                    views.append(label("• " + text[:110]
                                       + ("…" if len(text) > 110 else ""),
                                       11, wrap=True))
                    when = r.get("created_local") or ""
                    tags = [t for t in (r.get("tags") or [])
                            if not t.startswith("id:")][:2]
                    meta = when + ("  ·  " + ", ".join(tags) if tags else "")
                    views.append(label(meta, 9, secondary=True, truncate=True))
            views.append(divider())

            refresh = NSButton.buttonWithTitle_target_action_("Refresh", self,
                                                             "refresh:")
            quit_b = NSButton.buttonWithTitle_target_action_("Quit", self,
                                                            "quitApp:")
            if st["exists"] and not st["locked"]:
                lock = NSButton.buttonWithTitle_target_action_("Lock", self,
                                                               "lockNow:")
                change = NSButton.buttonWithTitle_target_action_(
                    "Change password", self, "startChangePw:")
                # All four on one row, with Quit holding the right edge. A
                # popover has a height ceiling, and a row carrying a single
                # button spends that ceiling on empty space.
                #
                # Measure this by RENDERING it, not by asking a button how
                # wide it would like to be: sizeToFit() on a standalone
                # NSButton reports well over what the same button occupies
                # once the stack view lays it out, which is enough to make a
                # row that fits comfortably look like it overflows.
                views.append(row(refresh, lock, change, _spacer(), quit_b,
                                 spacing=6))
            else:
                views.append(row(refresh, _spacer(), quit_b))

            return self._finish(views)

        # Not an action: PyObjC infers a selector from the method name, and
        # a name with no trailing underscores means "takes no arguments".
        @objc.python_method
        def _mount(self, host, body, h):
            for sub in list(host.subviews()):
                sub.removeFromSuperview()
            host.setFrameSize_((POPOVER_WIDTH, h))
            host.addSubview_(body)
            body.setFrameOrigin_((0, 0))

        def rebuild(self):
            """Refresh whichever surface is on screen - popover or window."""
            self.state = fetch_state(vault_path)
            body = self.buildBody()
            h = min(body.frame().size.height, POPOVER_MAX_HEIGHT)
            if self.window is not None and self.window.isVisible():
                self._mount(self.window.contentView(), body, h)
                self.window.setContentSize_((POPOVER_WIDTH, h))
            else:
                self.popover.setContentSize_((POPOVER_WIDTH, h))
                self._mount(self.popover.contentViewController().view(),
                            body, h)

        # ---- getting the thing on screen -------------------------------
        # Two surfaces, and which one to use is never in doubt:
        #
        #   clicked the status item -> popover. Reaching that handler means
        #       the icon was clicked, so the icon is on screen and there is
        #       something to anchor to. No geometry to guess at.
        #   everything else -> window. First launch, reopening the app, a
        #       second launch handing off: in all three the user has no
        #       reachable icon to click, which is how this app came to look
        #       broken in the first place.
        @objc.python_method
        def _makeWindow(self):
            win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                | NSWindowStyleMaskUtilityWindow,
                NSBackingStoreBuffered, False)
            win.setTitle_("Compartment")
            win.setReleasedWhenClosed_(False)
            win.setHidesOnDeactivate_(False)
            win.setLevel_(3)                            # NSFloatingWindowLevel
            return win

        @objc.python_method
        def _placeWindow(self):
            scr = NSScreen.mainScreen()
            if scr is None:
                self.window.center()
                return
            vf = scr.visibleFrame()
            f = self.window.frame()
            self.window.setFrameOrigin_((
                vf.origin.x + vf.size.width - f.size.width - 24,
                vf.origin.y + vf.size.height - f.size.height - 12))

        @objc.python_method
        def showWindow(self):
            _d("showing the window")
            if self.window is None:
                self.window = self._makeWindow()
            self.window.makeKeyAndOrderFront_(None)   # visible before rebuild,
            self.rebuild()                            # so rebuild targets it
            self._placeWindow()
            NSApp.activateIgnoringOtherApps_(True)

        @objc.python_method
        def showPopover(self):
            _d("showing the popover")
            if self.window is not None and self.window.isVisible():
                self.window.orderOut_(None)           # one surface at a time
            self.rebuild()
            btn = self.status_item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                btn.bounds(), btn, 1)                  # NSRectEdgeMaxY
            NSApp.activateIgnoringOtherApps_(True)

        # ---- actions ---------------------------------------------------
        def toggleHook_(self, sender):
            try:
                set_setting(vault_path, "capture_hook",
                            bool(sender.state()))
            except Exception:                           # noqa: BLE001
                pass
            self.rebuild()

        def toggleStarter_(self, sender):
            set_setting(vault_path, "search_starter_facts", bool(sender.state()))
            self.rebuild()

        def toggleExpire_(self, sender):
            set_setting(vault_path, "expire_memories", bool(sender.state()))
            self.rebuild()

        def changeAutoLock_(self, sender):
            idx = int(sender.selectedSegment())
            set_setting(vault_path, "auto_lock_minutes", AUTO_LOCK_CHOICES[idx])
            self.rebuild()

        def connectAgent_(self, sender):
            """Say it is working, then work.

            Wiring takes a second or two, and AppKit paints nothing while a
            handler is running. Doing the work here would freeze the panel
            for that second and then change one line, which is exactly what
            a dead button looks like. Painting first and working on the next
            turn of the run loop costs nothing and shows the user their click
            landed."""
            try:
                target, name = INTEGRATION_TARGETS[int(sender.tag())]
            except (IndexError, ValueError, AttributeError):
                return
            if self.connect_busy is not None:
                return                          # one at a time
            self.connect_busy = target
            self.connect_note = f"Connecting {name}…"
            self.rebuild()
            self.performSelector_withObject_afterDelay_(
                "runConnect:", target, 0.05)

        def runConnect_(self, target):
            try:
                _ok, msg = integrate(vault_path, str(target))
            except Exception as exc:            # noqa: BLE001
                # Never let this die silently: an exception raised inside an
                # action is swallowed by the run loop, and the button then
                # genuinely does nothing.
                msg = f"could not connect: {exc}"
            self.connect_busy = None
            self.connect_note = msg
            self.rebuild()

        def createVault_(self, sender):
            """Say it is working, then work.

            Seeding the starting memories and wiring the agents already on
            the machine takes a few seconds, and AppKit paints nothing while
            a handler runs. Painting first and working on the next turn of
            the run loop is what stops the only first-run button in the app
            from looking dead while it succeeds."""
            if self.creating or self.new_pw is None or self.new_repeat is None:
                return
            pw = str(self.new_pw.stringValue() or "")
            rep = str(self.new_repeat.stringValue() or "")
            if not pw or pw != rep:
                self.create_note = ("choose a passphrase" if not pw
                                    else "the two entries do not match")
                self.rebuild()
                return
            self.creating = True
            self.create_note = "Creating the vault…"
            self.rebuild()
            self.performSelector_withObject_afterDelay_("runCreate:", pw, 0.05)

        def runCreate_(self, passphrase):
            pw = str(passphrase)
            try:
                _ok, note = create_vault(vault_path, pw, pw)
            except Exception as exc:            # noqa: BLE001
                # An exception raised inside an action is swallowed by the
                # run loop, and the button then genuinely does nothing.
                note = f"could not create the vault: {exc}"
            self.creating = False
            self.create_note = note
            # Clear the fields whichever way it went: a passphrase must not
            # sit on screen after the attempt. rebuild() replaces them, and
            # on success there is no form to come back to at all.
            for f in (self.new_pw, self.new_repeat):
                if f is not None:
                    f.setStringValue_("")
            self.rebuild()

        def refresh_(self, sender):
            self.rebuild()

        def showFirst_(self, _):
            """First launch, reopen, and second-launch handoff all land here.

            Deliberately the window and never the popover. A popover is
            transient: it closes the instant anything takes focus, so on a
            first launch - when the user has never seen the icon, and the
            installer or a browser may still be grabbing focus - it can be
            gone before they ever notice it appeared. That is the exact
            failure this is meant to end. A window stays until dismissed.
            """
            self.showWindow()

        def lockNow_(self, sender):
            lock_vault(vault_path)
            self.unlock_note = None
            self.rebuild()

        def openDashboard_(self, sender):
            if open_dashboard(vault_path) is None:
                _d("the dashboard did not start")

        def unlockNow_(self, sender):
            if self.pw_field is None:
                return
            pw = str(self.pw_field.stringValue() or "")
            ok, note = unlock_vault(vault_path, pw)
            self.pw_field.setStringValue_("")        # never leave it on screen
            self.unlock_note = None if ok else note
            self.rebuild()

        def startChangePw_(self, sender):
            self.changing_pw = True
            self.change_note = None
            self.rebuild()

        def cancelChangePw_(self, sender):
            self.changing_pw = False
            self.change_note = None
            self.rebuild()

        def saveChangePw_(self, sender):
            if self.pw_new is None or self.pw_repeat is None:
                return
            new = str(self.pw_new.stringValue() or "")
            rep = str(self.pw_repeat.stringValue() or "")
            ok, note = change_passphrase(vault_path, new, rep)
            # Clear both regardless: a passphrase must not sit in a field on
            # screen after the attempt, successful or not.
            self.pw_new.setStringValue_("")
            self.pw_repeat.setStringValue_("")
            self.changing_pw = not ok
            self.change_note = note
            self.rebuild()

        def quitApp_(self, sender):
            stop_dashboard()
            NSApp.terminate_(self)

        def togglePopover_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
            elif self.window is not None and self.window.isVisible():
                self.window.orderOut_(None)
            else:
                self.showPopover()

    def _spacer():
        v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        v.setContentHuggingPriority_forOrientation_(1, 0)
        return v

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    _dbg = env("MENUBAR_DEBUG")
    def _d(*a):
        if _dbg: print("[menubar]", *a, file=sys.stderr, flush=True)
    _d("building controller (fetches state)…")
    ctrl = Controller.alloc().init()
    _d("controller ready")

    if render_to:
        # app.run() normally does this; without it the text system is not up.
        app.finishLaunching()
        body = ctrl.buildBody()
        body.layoutSubtreeIfNeeded()
        bounds = body.bounds()
        # Render through PDF, not cacheDisplayInRect. Caching a view that
        # belongs to no window gives you the switches and the segmented
        # control and not one glyph of text, because there is no window
        # backing store for the text system to lay glyphs into. Ordering an
        # offscreen window in does not help either: it draws as an inactive
        # window, so even the controls lose their tint. The PDF path records
        # glyphs vectorially with their fonts embedded and needs no window at
        # all, so it captures the panel exactly as designed. Rasterise at 2x
        # for a Retina-sharp asset.
        pdf = body.dataWithPDFInsideRect_(bounds)
        w, h = bounds.size.width, bounds.size.height
        image = NSImage.alloc().initWithData_(pdf)
        rep = (NSBitmapImageRep.alloc()
               .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                   None, int(w * 2), int(h * 2), 8, 4, True, False,
                   "NSCalibratedRGBColorSpace", 0, 0))
        rep.setSize_((w, h))
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        image.drawInRect_(NSMakeRect(0, 0, w, h))
        NSGraphicsContext.restoreGraphicsState()
        png = rep.representationUsingType_properties_(4, {})   # 4 = PNG
        ok = png.writeToFile_atomically_(render_to, True)
        print(f"rendered popover -> {render_to}" if ok
              else f"error: could not write {render_to}")
        return 0 if ok else 1
    item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1.0)
    item.setAutosaveName_("Compartment")          # remember where the user puts it
    # …but never remember it as hidden. An autosave name persists `visible`
    # as well as position, and a status item can be hidden by a stray
    # command-drag off the menu bar. Once that is saved the icon is gone for
    # good - every later launch restores it as hidden, the app looks like it
    # failed to start, and the only way back is to open the app. The icon
    # belongs in the menu bar for as long as Compartment is running; Quit is what
    # removes it.
    item.setVisible_(True)
    # Compartment's own mark - three nested squares, each turned further than
    # the one outside it - drawn by tools/make_icon.py and shipped as package
    # data. Marked as a template so macOS tints it for the light or dark bar
    # and inverts it on click, the way every other status item behaves.
    img = None
    mark = Path(__file__).resolve().parent / "data" / "menubar@2x.png"
    if mark.is_file():
        img = NSImage.alloc().initWithContentsOfFile_(str(mark))
        if img is not None:
            img.setSize_((MENUBAR_POINTS, MENUBAR_POINTS))
    if img is None:                       # a source checkout without the asset
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "square.split.2x2", "Compartment")
    if img is not None:
        img.setTemplate_(True)
        item.button().setImage_(img)
        item.button().setToolTip_("Compartment")
    else:
        item.button().setTitle_("▣")   # nested squares, if the asset is missing
    item.button().setTarget_(ctrl)
    item.button().setAction_("togglePopover:")

    vc = NSViewController.alloc().init()
    container = NSView.alloc().initWithFrame_(
        NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT))
    vc.setView_(container)
    pop = NSPopover.alloc().init()
    pop.setContentViewController_(vc)
    pop.setContentSize_((POPOVER_WIDTH, POPOVER_MAX_HEIGHT))
    pop.setBehavior_(1)                                 # transient
    pop.setAnimates_(True)

    ctrl.popover = pop
    ctrl.status_item = item
    _d("status item created; visible=", item.isVisible(),
       "image=", item.button().image() is not None,
       "window=", item.button().window() is not None)
    if _dbg:
        from AppKit import NSScreen
        import time as _t
        for _ in range(20):
            app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                0xFFFFFFFF, None, "kCFRunLoopDefaultMode", True)
            _t.sleep(0.05)
        w = item.button().window()
        _d("screen:", NSScreen.mainScreen().frame().size.width,
           "| item at:", w.frame().origin.x if w else "no window",
           "| visible:", item.isVisible())

    # Double-clicking Compartment.app while it is already running arrives here.
    # With no delegate macOS does nothing whatsoever, and for an app whose
    # only other surface is a status item that may be hidden behind the notch
    # that is indistinguishable from being broken. This is the way in that
    # cannot fail: the icon can be invisible, the bar can be full, and
    # opening the app again still shows the panel.
    # Conforming to the protocol is what makes this work, not decoration.
    # `hasVisibleWindows:` is a BOOL and the method returns a BOOL; with no
    # protocol to read the signature from, PyObjC types both as object
    # pointers, AppKit sees a method it cannot call, and the reopen event is
    # silently dropped - which is precisely the do-nothing behaviour above.
    try:
        _proto = [objc.protocolNamed("NSApplicationDelegate")]
    except Exception:                                   # noqa: BLE001
        _proto = []

    class CompartmentAppDelegate(NSObject, protocols=_proto):
        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, vis):
            _d("reopen event -> showing the panel")
            ctrl.showFirst_(None)
            return True

        def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
            # Closing the panel must never quit Compartment and take the menu bar
            # icon with it. AppKit already defaults to NO, but this app has
            # exactly one window and losing the icon by closing it would be
            # indistinguishable from the app crashing.
            return False

    delegate = CompartmentAppDelegate.alloc().init()
    app.setDelegate_(delegate)
    NSDistributedNotificationCenter.defaultCenter(
    ).addObserver_selector_name_object_(ctrl, "showFirst:",
                                        SHOW_NOTIFICATION, None)

    if show or claim_first_run(vault_path):
        ctrl.performSelector_withObject_afterDelay_("showFirst:", None, 0.35)
    _d("entering run loop")
    app.run()
    _d("run loop exited")
    return 0


__all__ = ["run", "self_check", "fetch_state", "read_settings", "set_setting",
           "summarise", "auto_lock_label", "default_vault", "compartment_bin",
           "AUTO_LOCK_CHOICES"]
