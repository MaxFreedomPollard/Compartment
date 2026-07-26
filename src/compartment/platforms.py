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
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, check=True).stdout
        import re
        m = re.search(r"sec = (\d+)", out)
        if m:
            return m.group(1)
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


def machine_id() -> str:
    """A stable identifier for this machine that does NOT change with
    network/hostname flaps (macOS renames the host per network). Falls back
    to hostname only if no platform id is available."""
    system = platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, check=True).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        elif system == "Linux":
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.exists(p):
                    return open(p).read().strip()
        elif system == "Windows":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        pass
    import socket
    return socket.gethostname()
