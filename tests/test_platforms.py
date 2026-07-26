"""Cross-platform primitives: file lock works, boot time is stable-in-session,
OS pack selection matches the running platform, console output survives a
legacy Windows code page."""
import io
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
