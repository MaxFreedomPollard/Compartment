"""Reading the boot instant on macOS and the BSDs: in-process, without a
fork on every vault open, and from every sysctl dialect."""
import ctypes
import ctypes.util
import platform
import subprocess

import pytest

from compartment import platforms


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))


class _RefusedLibc:
    """libc as Seatbelt leaves it: sysctlbyname is there and answers EPERM."""

    def sysctlbyname(self, *a):
        return -1


def _record_subprocesses(monkeypatch, stdout="", fail=False):
    calls = []

    def run(cmd, *a, **kw):
        calls.append(cmd)
        if fail:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")
    monkeypatch.setattr(platforms.subprocess, "run", run)
    return calls


def test_a_refused_kern_boottime_on_macos_does_not_fork_sysctl(monkeypatch):
    """Under Seatbelt (Claude Code, Codex) the in-process sysctl is refused,
    and the sysctl binary makes the same call and is refused as well. It was
    forked anyway on every vault open from the agent's shell."""
    monkeypatch.setattr(platforms.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: _RefusedLibc())
    calls = _record_subprocesses(monkeypatch, fail=True)
    assert platforms._sysctl_boot_estimate() is None
    assert calls == []


@pytest.mark.parametrize("system", ["FreeBSD", "NetBSD", "DragonFly", "Darwin"])
def test_the_in_process_read_runs_no_helper_to_find_libc(monkeypatch, system):
    """ctypes.util.find_library("c") runs /sbin/ldconfig -r on FreeBSD,
    OpenBSD and DragonFly and the C compiler on NetBSD, in a subprocess, on
    every call: the in-process read forked anyway."""
    looked_up = []
    monkeypatch.setattr(ctypes.util, "find_library",
                        lambda name: looked_up.append(name) or None)
    monkeypatch.setattr(platforms.platform, "system", lambda: system)
    _record_subprocesses(monkeypatch, fail=True)
    platforms._sysctl_boot_estimate()
    assert looked_up == []


@pytest.mark.skipif(platform.system() != "Linux",
                    reason="compares against /proc/stat")
def test_the_linux_clock_estimate_matches_the_kernel_btime():
    """The CLOCK_BOOTTIME fallback (Termux, a sandbox hiding /proc/stat) must
    name the boot instant the kernel reports, or a process that can read
    /proc/stat and one that cannot would pin different tokens."""
    try:
        with open("/proc/stat") as f:
            btime = next(float(line.split()[1]) for line in f
                         if line.startswith("btime "))
    except (OSError, StopIteration):
        pytest.skip("/proc/stat btime not readable here")
    clock = platforms._clock_boot_estimate()
    assert clock is not None and abs(clock[0] - btime) < 2


@pytest.mark.parametrize("stdout", [
    "1700000000\n",                                         # OpenBSD -n
    "{ sec = 1700000000, usec = 123456 } Tue Nov 14 22:13:20 2023\n",  # FreeBSD
])
def test_every_sysctl_dialect_of_kern_boottime_is_read(monkeypatch, stdout):
    """OpenBSD has no sysctlbyname, so the binary is its only kernel record,
    and `sysctl -n kern.boottime` prints the bare seconds there
    (sbin/sysctl/sysctl.c). Only FreeBSD's "sec = N" was understood, so
    OpenBSD always fell through to the clock."""
    monkeypatch.setattr(platforms.platform, "system", lambda: "OpenBSD")

    class NoSysctlbyname:
        pass
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: NoSysctlbyname())
    _record_subprocesses(monkeypatch, stdout=stdout)
    est = platforms._sysctl_boot_estimate()
    assert est is not None and est[0] == 1_700_000_000.0
