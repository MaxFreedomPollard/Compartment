"""Cross-platform primitives: file lock works, boot time is stable-in-session,
OS pack selection matches the running platform, console output survives a
legacy Windows code page."""
import contextlib
import io
import os
import platform
import socket
import subprocess
import sys

import pytest

from compartment import cli, platforms


def _legacy_console():
    """Stand-in for a Windows console on cp1252, which is what most installs
    give you and what CI runs under."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_legacy_console_would_crash_without_the_fix():
    """Guards the premise: if this ever stops raising, the test below proves
    nothing."""
    with pytest.raises(UnicodeEncodeError):
        stream = _legacy_console()
        stream.write("→")
        stream.flush()


@pytest.mark.parametrize("glyph", ["→", "✓", "⚠", "…",
                                   "·", "‖", "▣", "≈",
                                   "•"])
def test_cli_output_survives_a_legacy_code_page(glyph, monkeypatch):
    """`compartment init` once died on a single U+2192 after the vault was
    already written. No glyph the CLI prints may ever raise again."""
    out, err = _legacy_console(), _legacy_console()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    cli._utf8_console()
    print(f"  {glyph} vault ready")          # the exact shape of the crash
    print(glyph, file=sys.stderr)


def test_arbitrary_memory_text_cannot_break_output(monkeypatch):
    """`search` and `recent` print whatever the user stored, so the guarantee
    has to hold for text Compartment never chose."""
    out = _legacy_console()
    monkeypatch.setattr(sys, "stdout", out)
    cli._utf8_console()
    print("memory: café \U0001f600 中文 naïve “quoted”")


def test_boot_time_stable_within_session():
    a = platforms.boot_time()
    b = platforms.boot_time()
    assert a == b and a.isdigit()  # same boot → identical, numeric epoch


# ---------------------------------------------------------------------------
# The boot token is hashed into the session credential's wrap key, and
# session.get() deletes a credential its key will not open. The kernels
# derive the boot instant from the wall clock, so an NTP STEP moves it -
# which is why the token is pinned per boot and a moved reading must never
# reach the key.
# ---------------------------------------------------------------------------

def _tmp_session_dir(monkeypatch, tmp_path):
    d = tmp_path / "session"
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(d))
    return d


def test_boot_token_pins_the_first_value_seen(tmp_path, monkeypatch):
    d = _tmp_session_dir(monkeypatch, tmp_path)
    tok = platforms._pinned_boot_token(1000.0, 50.0, d / ".boottime")
    assert tok == "1000"
    assert (d / ".boottime").is_file()


def test_a_clock_step_cannot_move_the_boot_token(tmp_path, monkeypatch):
    """The CI-runner case, and every NTP step on a live machine: the derived
    boot instant moves a few seconds while uptime keeps growing. The pinned
    token must hold, or the credential sealed under the old value dies."""
    d = _tmp_session_dir(monkeypatch, tmp_path)
    marker = d / ".boottime"
    first = platforms._pinned_boot_token(1000.0, 50.0, marker)
    stepped = platforms._pinned_boot_token(1003.0, 51.0, marker)   # +3s step
    slewed = platforms._pinned_boot_token(999.2, 52.0, marker)     # slew back
    assert first == stepped == slewed == "1000"


def test_a_reboot_moves_the_boot_token(tmp_path, monkeypatch):
    """Shrunk uptime is the reboot signature; the token must change so the
    restart genuinely relocks."""
    d = _tmp_session_dir(monkeypatch, tmp_path)
    marker = d / ".boottime"
    assert platforms._pinned_boot_token(1000.0, 500.0, marker) == "1000"
    assert platforms._pinned_boot_token(1490.0, 10.0, marker) == "1490"


def test_boot_time_survives_a_stepped_kernel_reading(tmp_path, monkeypatch):
    """End to end through boot_time(): the platform estimate moves 3 seconds
    between two calls, the answer does not."""
    _tmp_session_dir(monkeypatch, tmp_path)
    readings = iter([(2000.0, 100.0), (2003.0, 101.0)])
    monkeypatch.setattr(platforms, "_posix_boot_estimate",
                        lambda: next(readings))
    monkeypatch.setattr(platforms.platform, "system", lambda: "Linux")
    assert platforms.boot_time() == platforms.boot_time() == "2000"


def test_a_clock_step_does_not_kill_the_session_credential(tmp_path, monkeypatch):
    """The whole point, at the session layer: a credential stored before a
    3-second clock step still opens after it, and the file survives. Before
    the pin, the moved reading re-keyed the wrap key and get() deleted the
    credential - a vault that was unlocked stayed locked until the
    passphrase was typed again."""
    from compartment import session
    _tmp_session_dir(monkeypatch, tmp_path)
    base = [(3000.0, 100.0)]
    monkeypatch.setattr(platforms, "_posix_boot_estimate", lambda: base[0])
    monkeypatch.setattr(platforms.platform, "system", lambda: "Linux")
    vault = str(tmp_path / "v.vault")
    key = b"k" * 32
    session.store(vault, key)
    base[0] = (3003.0, 101.0)                                      # the step
    assert session.get(vault) == key
    assert session._file_for(vault).is_file()


def test_filelock_is_exclusive(tmp_path):
    lockpath = str(tmp_path / "x.flock")
    with platforms.FileLock(lockpath, timeout=0.3):
        with pytest.raises(Exception):
            with platforms.FileLock(lockpath, timeout=0.3):
                pass  # second acquisition must fail while first is held


def test_filelock_reacquire_after_release(tmp_path):
    lockpath = str(tmp_path / "y.flock")
    with platforms.FileLock(lockpath, timeout=1):
        pass
    with platforms.FileLock(lockpath, timeout=1):  # released → acquirable again
        pass


# ---------------------------------------------------------------------------
# machine_id(): it is folded into the boot-session credential's wrap key, and
# session.get() deletes a credential its key will not open. So an answer that
# merely DIFFERS from the one used at store time is destructive, and the only
# safe response to a lookup it could not complete is to raise.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_session_dir(tmp_path, monkeypatch):
    """The per-boot marker lives in the session directory. No test in this
    file may pin anything into the real one."""
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))
    return tmp_path / "sess"


def test_a_failed_lookup_raises_rather_than_substituting_the_hostname():
    """The whole defect in one assertion. A wedged ioreg used to be answered
    with socket.gethostname(), which cannot rebuild the stored wrap key, so
    session.get() read a good credential as dead and unlinked it."""
    with pytest.raises(RuntimeError):
        with _lookup(raises=RuntimeError("ioreg could not be read")):
            platforms.machine_id()


def test_no_platform_id_at_all_still_falls_back_to_the_hostname():
    """A system that genuinely has none - a container with no machine-id -
    keeps the documented fallback. There the hostname is the stable answer,
    not a substitute for one."""
    with _lookup(returns=None):
        assert platforms.machine_id() == socket.gethostname()


def test_the_id_is_pinned_for_the_life_of_the_boot():
    """Once one process of this boot has an answer, a later failure in the
    lookup cannot move it - which is what makes the relock stop recurring
    rather than merely stop being destructive."""
    with _lookup(returns="THE-REAL-UUID"):
        assert platforms.machine_id() == "THE-REAL-UUID"
    with _lookup(raises=RuntimeError("ioreg timed out")):
        assert platforms.machine_id() == "THE-REAL-UUID"


def test_the_hostname_fallback_is_pinned_too(monkeypatch):
    """macOS renames the host per network. Where the hostname is all there is,
    a rename mid-session must not re-key the credential either."""
    with _lookup(returns=None):
        first = platforms.machine_id()
    monkeypatch.setattr(socket, "gethostname", lambda: "renamed-by-dhcp")
    with _lookup(returns=None):
        assert platforms.machine_id() == first != "renamed-by-dhcp"


def test_a_different_boot_does_not_reuse_the_pin(monkeypatch):
    """The marker is scoped to the boot that wrote it, so a copy of the
    session directory is inert on any other machine or boot."""
    monkeypatch.setattr(platforms, "boot_time", lambda: "1000")
    with _lookup(returns="FIRST-BOOT-ID"):
        assert platforms.machine_id() == "FIRST-BOOT-ID"
    monkeypatch.setattr(platforms, "boot_time", lambda: "2000")
    with _lookup(returns="SECOND-BOOT-ID"):
        assert platforms.machine_id() == "SECOND-BOOT-ID"


def test_an_unwritable_marker_costs_a_lookup_not_correctness(
        tmp_path, monkeypatch):
    """Pinning is best effort. Where the marker cannot be written the answer
    is still right; only the saving of it is lost."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setattr(platforms, "_machine_marker",
                        lambda: blocker / "sub" / ".machineid")
    with _lookup(returns="STILL-CORRECT"):
        assert platforms.machine_id() == "STILL-CORRECT"
        assert platforms._pinned_machine_id(platforms.boot_time()) is None


# --- the platform branches themselves ---------------------------------------

@pytest.mark.skipif(platform.system() != "Darwin", reason="ioreg is macOS")
@pytest.mark.parametrize("boom", [
    subprocess.TimeoutExpired(["ioreg"], 5.0),       # the machine is loaded
    subprocess.CalledProcessError(1, ["ioreg"]),     # non-zero exit
    OSError("No such file or directory: 'ioreg'"),   # missing binary
])
def test_every_ioreg_failure_raises(boom, monkeypatch):
    real = subprocess.run

    def wedged(cmd, *a, **k):
        if cmd and cmd[0] == "ioreg":
            raise boom
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", wedged)
    with pytest.raises(RuntimeError):
        platforms._platform_id()


@pytest.mark.skipif(platform.system() != "Darwin", reason="ioreg is macOS")
def test_a_healthy_ioreg_gives_a_uuid_not_the_hostname():
    value = platforms._platform_id()
    assert value and value != socket.gethostname()


def test_an_empty_machine_id_file_is_not_an_id(tmp_path, monkeypatch):
    """An empty /etc/machine-id is systemd's documented generate-on-first-boot
    state and several container base images ship one. Returning "" from it
    shadowed both the dbus path below and the hostname fallback."""
    empty, dbus = tmp_path / "machine-id", tmp_path / "dbus-id"
    empty.write_text("")
    dbus.write_text("dbus-fallback-id\n")
    with _linux(monkeypatch, empty, dbus):
        assert platforms._platform_id() == "dbus-fallback-id"


def test_no_machine_id_file_anywhere_is_no_id_rather_than_an_error(
        tmp_path, monkeypatch):
    with _linux(monkeypatch, tmp_path / "absent", tmp_path / "gone"):
        assert platforms._platform_id() is None


def test_an_unreadable_machine_id_file_raises(tmp_path, monkeypatch):
    """It exists, so this system HAS an id; we merely could not read it. That
    is the case that must never be answered with the hostname.

    The unreadable thing is a DIRECTORY named machine-id: open() refuses that
    with an OSError on every platform and for every user. The previous shape,
    a file chmod'd to 0o000, read back fine in the two places this test also
    runs - as root, and on Windows, whose chmod cannot drop read permission -
    so it proved the contract on some machines and failed it on others."""
    blocked = tmp_path / "machine-id"
    blocked.mkdir()
    with _linux(monkeypatch, blocked, tmp_path / "absent"):
        with pytest.raises(RuntimeError):
            platforms._platform_id()


def test_a_failed_registry_read_raises_on_windows(monkeypatch):
    """The Windows branch carries the same contract as the file branch: a
    MachineGuid that cannot be READ is a failed lookup, never a hostname
    substitution. Faked through sys.modules so the branch runs on every
    host."""
    import types
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()

    def deny(*a, **k):
        raise PermissionError(13, "registry access denied")
    fake.OpenKey = deny
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(platforms.platform, "system", lambda: "Windows")
    with pytest.raises(RuntimeError):
        platforms._platform_id()


def test_a_missing_machineguid_is_no_id_rather_than_an_error(monkeypatch):
    """No key at all means this system HAS no id - the documented hostname
    case, not a fault."""
    import types
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()

    def absent(*a, **k):
        raise FileNotFoundError(2, "no such registry key")
    fake.OpenKey = absent
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(platforms.platform, "system", lambda: "Windows")
    assert platforms._platform_id() is None


# --- helpers ----------------------------------------------------------------

@contextlib.contextmanager
def _lookup(returns=None, raises=None):
    """Stand in for the platform lookup, so these tests are about machine_id's
    contract rather than about the machine they run on."""
    real = platforms._platform_id
    platforms._platform_id = (
        (lambda: (_ for _ in ()).throw(raises)) if raises else (lambda: returns))
    try:
        yield
    finally:
        platforms._platform_id = real


@contextlib.contextmanager
def _linux(monkeypatch, *paths):
    """Run the Linux branch against files we control, on any host."""
    monkeypatch.setattr(platforms.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platforms, "_LINUX_MACHINE_ID_PATHS",
                        tuple(str(p) for p in paths))
    yield
