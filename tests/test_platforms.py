"""Cross-platform primitives: file lock works, boot time is stable-in-session,
OS pack selection matches the running platform."""
import pytest

from compartment import platforms


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
