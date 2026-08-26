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


@pytest.mark.skipif(hasattr(os, "getuid") and os.getuid() == 0,
                    reason="root reads unreadable files")
def test_an_unreadable_machine_id_file_raises(tmp_path, monkeypatch):
    """It exists, so this system HAS an id; we merely could not read it. That
    is the case that must never be answered with the hostname."""
    blocked = tmp_path / "machine-id"
    blocked.write_text("real-id\n")
    blocked.chmod(0o000)
    try:
        with _linux(monkeypatch, blocked, tmp_path / "absent"):
            with pytest.raises(RuntimeError):
                platforms._platform_id()
    finally:
        blocked.chmod(0o600)


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
