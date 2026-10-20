"""FileLock on filesystems that cannot lock: NFS without its lock service or
mounted nolock, some SMB and FUSE mounts. That is not "busy", and waiting
does not change it."""
import errno
import os
import time

import pytest

from compartment import platforms
from compartment.crypto import CryptoError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fcntl branch")


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL])
def test_a_filesystem_without_locks_is_named_at_once_not_called_busy(
        tmp_path, monkeypatch, code):
    """Before: every write waited out the timeout and then reported "Vault
    is busy: another process holds the write lock", on a vault nobody else
    had open."""
    def no_locks(fh, op):
        raise OSError(code, os.strerror(code))
    monkeypatch.setattr(platforms.fcntl, "flock", no_locks)
    started = time.monotonic()
    with pytest.raises(CryptoError) as err:
        with platforms.FileLock(str(tmp_path / "v.vault.flock"), timeout=3):
            pass
    assert time.monotonic() - started < 1.5
    assert "busy" not in str(err.value)
    assert "does not support file locks" in str(err.value)
    assert "local disk" in str(err.value)


@pytest.mark.parametrize("code", [errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES])
def test_a_lock_someone_holds_is_still_waited_for_and_called_busy(
        tmp_path, monkeypatch, code):
    """BSD flock answers EWOULDBLOCK, and Linux's NFS emulation can answer
    EAGAIN or EACCES: all three are another holder."""
    def held(fh, op):
        raise OSError(code, os.strerror(code))
    monkeypatch.setattr(platforms.fcntl, "flock", held)
    with pytest.raises(CryptoError, match="busy"):
        with platforms.FileLock(str(tmp_path / "v.vault.flock"), timeout=0.2):
            pass
