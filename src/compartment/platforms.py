"""Cross-platform primitives so Compartment runs natively on macOS, Linux, and
Windows: an advisory exclusive file lock, and the boot-session identity used
by the locked-by-default credential.

Everything platform-specific is isolated here; the rest of the codebase calls
these functions and never imports fcntl / msvcrt / sysctl directly.
"""
from __future__ import annotations

from .home import env, home
import json
import os
import platform
import subprocess
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Both helpers below sit on the vault-open path, so every subprocess they run
# is bounded. A wedged system binary degrades to the documented fallback
# instead of hanging the caller indefinitely.
_SUBPROCESS_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Advisory exclusive file lock (context manager over an open file handle)
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    import msvcrt

    def _lock_nb(fh) -> bool:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_nb(fh) -> bool:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass


class FileLock:
    """Cross-platform advisory exclusive lock on a lock file path."""

    def __init__(self, path: str, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while not _lock_nb(self._fh):
            if time.time() > deadline:
                self._fh.close()
                from .crypto import CryptoError
                raise CryptoError(
                    f"Vault is busy: another process holds the write lock "
                    f"(waited {self.timeout:.0f}s)")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Boot time - changes on every restart/power loss (basis of the session cred)
# ---------------------------------------------------------------------------

def boot_time() -> str:
    """Seconds-since-epoch of the current boot as a string. Distinct after
    every restart, so a credential wrapped with it dies on reboot."""
    system = platform.system()
    if system == "Darwin":
        # Guarded like the other branches, and bounded: this runs on every
        # vault open, so a missing or wedged sysctl must fall through to the
        # actionable error below instead of raising something opaque or
        # hanging the process forever.
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True, check=True,
                                 timeout=_SUBPROCESS_TIMEOUT).stdout
            import re
            m = re.search(r"sec = (\d+)", out)
            if m:
                return m.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    elif system == "Linux":
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        return line.split()[1].strip()
        except OSError:
            pass
    elif system == "Windows":
        try:
            return _windows_boot_token()
        except Exception:
            pass
    raise RuntimeError(
        "Cannot determine boot time on this platform; use --keychain (macOS) "
        "or COMPARTMENT_PASSPHRASE instead of the boot-session credential")


def _win_marker() -> Path:
    base = Path(env("SESSION_DIR", home() / "session"))
    return base / ".winboot"


def _windows_boot_token() -> str:
    """A per-boot token, stable across processes for the life of one boot and
    new on the next boot.

    Windows has no cheap, in-process, offline kernel boot *timestamp* (WMI/CIM
    is a slow subprocess, and this runs on every vault open). So we derive the
    boot instant as wall_clock - uptime and PIN the first value seen this boot
    to a marker file, reusing it while the machine is still up. Naively
    recomputing `now - uptime` on every call silently re-keys the session
    credential and relocks the vault mid-session in three ways this avoids:
      1. sub-second int() truncation flapping between two processes;
      2. wall-clock drift after NTP slew accumulating past a second boundary;
      3. a jump of the whole sleep duration after every sleep/resume
         (GetTickCount64 excludes sleep; time.time() does not).
    A real reboot resets uptime and moves the boot instant far beyond the
    tolerance below, so it is detected and the token changes, as intended."""
    import ctypes
    uptime_s = ctypes.windll.kernel32.GetTickCount64() / 1000.0  # excludes sleep
    computed = time.time() - uptime_s                            # ~boot instant
    marker = _win_marker()
    try:
        rec = json.loads(marker.read_text(encoding="utf-8"))
        same_boot = (uptime_s + 2.0 >= float(rec["uptime"])       # uptime only grows within a boot
                     and abs(computed - float(rec["boot"])) <= 300.0)  # tolerate NTP slew
        if same_boot:
            return str(int(float(rec["boot"])))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # First sighting of this boot (or a detected reboot / unreadable marker):
    # pin the freshly computed value for every later process to reuse.
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(json.dumps({"boot": computed, "uptime": uptime_s}),
                       encoding="utf-8")
        os.replace(tmp, marker)
    except OSError:
        pass
    return str(int(computed))


# ---------------------------------------------------------------------------
# Machine identity - bound into the boot-session credential as CONTEXT
#
# Load-bearing in a way its size hides. session._boot_context() folds this
# value into the wrap key of the stored unlock credential, and session.get()
# DELETES that credential when the key it rebuilds does not open it. So a
# machine_id() that answers with a different value than the one used at store
# time does not degrade gracefully: it destroys the credential, relocks the
# vault for every process holding it, and leaves it locked after the
# underlying fault clears, until the passphrase is typed again.
#
# Hence the two rules below, which are what boot_time() has always done:
#
#   1. A lookup that FAILED raises, and never substitutes. Answering with the
#      hostname after a wedged ioreg or an unreadable machine-id file swaps in
#      a value that cannot match what was stored, which is exactly the
#      destructive case. Raising surfaces as CryptoError instead, which leaves
#      the credential alone: the vault reports locked for that one call and
#      opens again the moment the fault clears.
#   2. A system that genuinely HAS no platform id still gets the documented
#      hostname fallback, because there the hostname is the stable answer
#      rather than a substitute for one.
#
# On top of both, the answer is PINNED per boot, so every process of this boot
# agrees on one value and no later hiccup in the lookup can move it.
# ---------------------------------------------------------------------------

#: Read in order; the first non-empty one wins. A module constant so a test
#: can point it somewhere writable instead of needing a container.
_LINUX_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def _machine_marker() -> Path:
    base = Path(env("SESSION_DIR", home() / "session"))
    return base / ".machineid"


def _pinned_machine_id(boot: str) -> str | None:
    """The id an earlier process of THIS boot already settled on, if any."""
    try:
        rec = json.loads(_machine_marker().read_text(encoding="utf-8"))
        if rec["boot"] == boot and isinstance(rec["id"], str) and rec["id"]:
            return rec["id"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _pin_machine_id(boot: str, value: str) -> None:
    """Best effort: an unwritable marker costs a lookup next time, not
    correctness.

    Writing the id down gives away nothing. It is public - anything running as
    this user can run ioreg or read /etc/machine-id - and it is only CONTEXT
    for a wrap key whose actual secret is 32 random bytes in a volatile kernel
    holder that no filesystem backs. The marker is also useless on another
    machine, because it is rejected unless it names the current boot. That is
    the same argument _windows_boot_token() already pins its own marker on."""
    marker = _machine_marker()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(marker.parent, 0o700)
        except OSError:
            pass
        tmp = marker.with_name(marker.name + ".tmp")
        # 0600 from the start rather than write-then-chmod, which publishes
        # the file at whatever the umask allows first. The session directory
        # is already 0700, so this is defence in depth, and it keeps the
        # marker the same mode as the credentials beside it.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"boot": boot, "id": value}).encode())
        finally:
            os.close(fd)
        os.replace(tmp, marker)          # atomic; a torn marker never appears
    except OSError:
        pass


def _platform_id() -> str | None:
    """This machine's OS-assigned identifier, or None where the system
    genuinely has none.

    Raises RuntimeError if the lookup itself failed, so the caller can tell
    "there is no id here" from "I could not find out"."""
    system = platform.system()
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, check=True,
                timeout=_SUBPROCESS_TIMEOUT).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            # Missing binary, non-zero exit, or _SUBPROCESS_TIMEOUT exceeded
            # because the machine is loaded. Every one of those is transient,
            # and none is evidence about this machine's identity.
            raise RuntimeError(f"ioreg could not be read ({exc})") from exc
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                parts = line.split('"')
                if len(parts) >= 2 and parts[-2]:
                    return parts[-2]
        return None
    if system == "Linux":
        failed = None
        for p in _LINUX_MACHINE_ID_PATHS:
            try:
                with open(p) as f:
                    value = f.read().strip()
            except FileNotFoundError:
                continue                 # this system does not use that path
            except OSError as exc:       # it is there, we could not read it
                failed = failed or exc
                continue
            if value:
                return value
            # An EMPTY /etc/machine-id is systemd's documented
            # generate-on-first-boot state, and several container base images
            # ship one. It is not an id, so keep looking: returning "" here
            # used to shadow both the dbus path and the hostname fallback.
        if failed is not None:
            raise RuntimeError(
                f"a machine-id file exists but could not be read ({failed})")
        return None
    if system == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                value = winreg.QueryValueEx(k, "MachineGuid")[0]
        except FileNotFoundError:        # no such key or value on this install
            return None
        except OSError as exc:           # anything else is a failed read
            raise RuntimeError(
                f"MachineGuid could not be read ({exc})") from exc
        return value if isinstance(value, str) and value else None
    return None


def machine_id() -> str:
    """A stable identifier for this machine that does NOT change with
    network/hostname flaps (macOS renames the host per network).

    Pinned for the life of the boot, so every process agrees on one value and
    a later hiccup in the lookup cannot move it. Falls back to the hostname
    only where the system genuinely has no platform id - and pins that too, so
    a rename cannot move it either.

    Raises RuntimeError when it cannot tell, like boot_time() and for the same
    reason: session._boot_context() turns that into a CryptoError, which
    leaves the stored credential untouched, whereas substituting a different
    value here gets that credential deleted."""
    boot = boot_time()
    pinned = _pinned_machine_id(boot)
    if pinned is not None:
        return pinned
    value = _platform_id()
    if value is None:
        import socket
        value = socket.gethostname()
    _pin_machine_id(boot, value)
    return value
