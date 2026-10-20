"""Cross-platform primitives so Compartment runs natively on macOS, Linux, and
Windows: an advisory exclusive file lock, and the boot-session identity used
by the locked-by-default credential.

Everything platform-specific is isolated here; the rest of the codebase calls
these functions and never imports fcntl / msvcrt / sysctl directly.
"""
from __future__ import annotations

from .home import env, home
import errno
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

# Only these errors mean "someone else holds it". Anything else means the
# filesystem cannot lock at all - NFS whose lock service is not running or
# that is mounted `nolock`/`nolocks` (ENOLCK, ENOTSUP), some FUSE and SMB
# mounts (EOPNOTSUPP, EINVAL) - and no amount of waiting changes that.
# Treating those as busy made every write on such a vault wait out the
# timeout and then blame "another process", which sent people hunting for a
# process that did not exist.
if IS_WINDOWS:
    import msvcrt

    _BUSY = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", 36)})

    def _lock_nb(fh) -> bool:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in _BUSY:
                return False
            raise

    def _unlock(fh) -> None:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    _BUSY = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})

    def _lock_nb(fh) -> bool:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in _BUSY:
                return False
            raise

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
        while True:
            try:
                if _lock_nb(self._fh):
                    return self
            except OSError as exc:
                self._fh.close()
                self._fh = None
                from .crypto import CryptoError
                raise CryptoError(
                    f"Cannot lock {self.path}: the filesystem it is on does "
                    f"not support file locks ({exc.strerror or exc}). That is "
                    f"usually a network share - NFS without its lock service "
                    f"or mounted nolock, or an SMB mount without byte-range "
                    f"locks. Keep the vault on a local disk (--vault or "
                    f"COMPARTMENT_VAULT), or enable locking on the "
                    f"mount.") from exc
            if time.time() > deadline:
                self._fh.close()
                self._fh = None
                from .crypto import CryptoError
                raise CryptoError(
                    f"Vault is busy: another process holds the write lock "
                    f"(waited {self.timeout:.0f}s)")
            time.sleep(0.05)

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
    every restart, so a credential wrapped with it dies on reboot.

    Pinned for the life of the boot on every platform, the way Windows always
    pinned its own. The kernels do not keep the boot instant fixed: Linux
    derives `btime` and macOS `kern.boottime` from the wall clock, so an NTP
    STEP - which chrony performs early on every freshly booted VM, and which
    follows many a suspend/resume - moves the reported boot time by the size
    of the step. This value is hashed into the session credential's wrap key,
    and `session.get` deletes a credential its key will not open, so an
    unpinned reading meant one clock step relocked every vault on the machine
    and destroyed the stored credential with it."""
    system = platform.system()
    if system == "Windows":
        try:
            return _windows_boot_token()
        except Exception:
            pass
    else:
        est = _posix_boot_estimate()
        if est is not None:
            return _pinned_boot_token(est[0], est[1], _posix_marker())
    raise RuntimeError(
        "Cannot determine boot time on this platform; use --keychain (macOS) "
        "or COMPARTMENT_PASSPHRASE instead of the boot-session credential")


def _posix_boot_estimate() -> tuple[float, float] | None:
    """(boot instant, uptime seconds) from the platform, or None.

    Guarded and bounded: this runs on every vault open, so a missing or
    wedged source must fall through to boot_time's actionable error instead
    of raising something opaque or hanging the process forever.

    The uptime source is deliberately step-immune, which is what lets
    `_pinned_boot_token` tell a stepped clock (uptime kept growing) from a
    real reboot (uptime shrank). Linux /proc/uptime counts from boot
    regardless of wall-clock changes; on macOS the pair is derived from one
    `kern.boottime` reading, so a step moves both ends equally and cancels.
    """
    system = platform.system()
    if system == "Darwin" or system.endswith("BSD") or system == "DragonFly":
        est = _sysctl_boot_estimate()
        if est is not None:
            return est
    elif system == "Linux":
        try:
            btime = None
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        btime = float(line.split()[1])
                        break
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            if btime is not None:
                return btime, up
        except (OSError, ValueError):
            pass
    # The kernel's own boot record could not be read. That is the normal
    # state inside an agent's sandbox - Claude Code and Codex run shell
    # commands under macOS Seatbelt, which refuses kern.boottime - and on
    # Android, where SELinux hides /proc/stat from apps such as Termux. A
    # clock that counts from boot answers the same question in-process.
    return _clock_boot_estimate()


def _sysctl_boot_estimate() -> tuple[float, float] | None:
    """kern.boottime on macOS and the BSDs: in-process first, so a vault open
    does not fork, then through the sysctl binary for a libc without
    sysctlbyname (OpenBSD)."""
    import struct
    try:
        import ctypes
        # The process's own symbols, which include libc's. Not
        # ctypes.util.find_library("c"): on FreeBSD, OpenBSD and DragonFly
        # that runs /sbin/ldconfig -r, and on NetBSD it runs the C compiler,
        # in a subprocess, on every call - a fork on every vault open, which
        # is the thing reading this in-process was meant to avoid.
        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(32)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctlbyname(b"kern.boottime", buf, ctypes.byref(size), None, 0) == 0:
            # struct timeval: a 64-bit time_t on every 64-bit system, and a
            # 32-bit one where the whole struct is 8 bytes.
            fmt = "=q" if size.value >= 16 else "=i"
            sec = float(struct.unpack_from(fmt, buf.raw)[0])
            if sec > 0:
                return sec, time.time() - sec
    except (ImportError, OSError, AttributeError, ValueError, TypeError,
            struct.error):
        pass
    if platform.system() == "Darwin":
        # macOS always has sysctlbyname, and the binary makes the same call:
        # where the call above was refused - Seatbelt, under Claude Code and
        # Codex - the binary is refused too. Forking it anyway cost every
        # vault open in an agent's shell a process and a sandbox-violation
        # report, for an answer that could not come.
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, check=True,
                             timeout=_SUBPROCESS_TIMEOUT).stdout
        import re
        m = re.search(r"sec = (\d+)", out)
        if m:
            sec = float(m.group(1))
            return sec, time.time() - sec
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _clock_boot_estimate() -> tuple[float, float] | None:
    """(boot instant, uptime) from a clock that starts at boot, or None.

    Only clocks that keep counting through sleep qualify, or every resume
    would read as a reboot: CLOCK_BOOTTIME on Linux, Android and OpenBSD, and
    CLOCK_MONOTONIC on macOS. There libc computes CLOCK_MONOTONIC as the wall
    clock minus the kernel's boot time, read from the commpage, which no
    sandbox gates; so wall clock minus it IS kern.boottime, to the
    microsecond (checked under Seatbelt: constant across calls, and 8.6 s
    away from mach_continuous_time, CLOCK_MONOTONIC_RAW, on a machine up
    for two days). The result feeds _pinned_boot_token like any other
    estimate, so the first value seen this boot is the one every process
    agrees on."""
    names = ("CLOCK_MONOTONIC",) if platform.system() == "Darwin" else (
        "CLOCK_BOOTTIME",)
    for name in names:
        clock = getattr(time, name, None)
        if clock is None:
            continue
        try:
            up = time.clock_gettime(clock)
        except OSError:
            continue
        if up > 0:
            return time.time() - up, up
    return None


def _posix_marker() -> Path:
    """The POSIX pin. Windows keeps its own `.winboot` - renaming a marker
    that live installs already rely on would re-key their credentials."""
    base = Path(env("SESSION_DIR", home() / "session"))
    return base / ".boottime"


def _pinned_boot_token(computed: float, uptime_s: float, marker: Path) -> str:
    """First value seen this boot, pinned; reused while the machine is up.

    Naively returning the platform's own reading re-keys the session
    credential whenever that reading moves, which relocks the vault
    mid-session in three ways this avoids:
      1. sub-second int() truncation flapping between two processes;
      2. wall-clock steps and NTP slew moving the derived boot instant;
      3. a jump of the whole sleep duration where the uptime source
         excludes sleep (Windows GetTickCount64).
    A real reboot resets uptime and moves the boot instant far beyond the
    tolerance below, so it is detected and the token changes, as intended."""
    try:
        rec = json.loads(marker.read_text(encoding="utf-8"))
        same_boot = (uptime_s + 2.0 >= float(rec["uptime"])       # uptime only grows within a boot
                     and abs(computed - float(rec["boot"])) <= 300.0)  # tolerate steps and slew
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
    return _pinned_boot_token(computed, uptime_s, _win_marker())


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


def file_signature(path) -> tuple | None:
    """(inode, size, modification time in ns) for a file, or None when it is
    not there. Two signatures that are equal mean the file was not written
    in between, which is what lets a watcher skip re-reading it.

    All three, because each misses something alone: an append within one
    clock tick keeps the mtime, an atomic replace can keep the size, and the
    inode is the only one that shows a file was swapped for another."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)
