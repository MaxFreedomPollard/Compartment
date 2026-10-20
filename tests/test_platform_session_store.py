"""Where the session store cannot be written: a container run as an arbitrary
uid (HOME is `/`), a read-only root, and an agent's shell under macOS
Seatbelt, which may read the user's session directory but not write it.

Reading a credential, or finding there is none, must not need a write."""
import errno
import os

import pytest

from compartment import session
from compartment.vault import Vault

from conftest import PASS


def _unmakeable_store(tmp_path, monkeypatch):
    """A session directory nobody can create: its parent is a regular file.
    mkdir refuses that for every user, root included, on every platform."""
    blocker = tmp_path / "home-is-not-writable"
    blocker.write_text("")
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(blocker / "session"))
    return blocker / "session"


def test_finding_no_credential_needs_no_session_directory(tmp_path, monkeypatch):
    store = _unmakeable_store(tmp_path, monkeypatch)
    vault = str(tmp_path / "v.vault")
    assert session.get(vault) is None
    assert session.clear(vault) is False          # `compartment lock`
    assert not store.exists()


def test_the_passphrase_variable_opens_a_vault_where_home_is_not_writable(
        tmp_path, monkeypatch):
    """`docker run --user 1000 -e COMPARTMENT_PASSPHRASE ...`: HOME is `/`, so
    the session store would be /.compartment/session. Every open used to die
    with PermissionError making it, before the variable was ever read."""
    vault = str(tmp_path / "v.vault")
    Vault.create(vault, PASS, creator="test").lock()
    _unmakeable_store(tmp_path, monkeypatch)
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)
    assert Vault.find_credential(vault) == (PASS, None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_store_that_refuses_chmod_still_yields_its_credential(
        tmp_path, monkeypatch):
    """The agent-sandbox case. The user unlocked in a terminal; the agent's
    `compartment search` runs under Seatbelt, which lets it read the 0700
    session directory but answers chmod on it with EPERM (seen in Claude
    Code's own sandbox). The credential is there and readable, so the
    lookup must find it rather than die on a mode it did not need to set."""
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))
    vault = str(tmp_path / "v.vault")
    key = b"k" * 32
    session.store(vault, key)
    real_chmod = os.chmod

    def seatbelt(path, mode, *a, **kw):
        if os.fspath(path).startswith(str(tmp_path / "sess")):
            raise PermissionError(errno.EPERM, "Operation not permitted",
                                  os.fspath(path))
        return real_chmod(path, mode, *a, **kw)
    monkeypatch.setattr(session.os, "chmod", seatbelt)
    assert session.get(vault) == key
    assert session.credential_path(vault).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_store_with_a_wide_mode_is_still_narrowed(tmp_path, monkeypatch):
    """Skipping a chmod that is not needed must not skip one that is."""
    d = tmp_path / "sess"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(d))
    session.store(str(tmp_path / "v.vault"), b"k" * 32)
    assert (d.stat().st_mode & 0o777) == 0o700


# --- no usable holder: the refusal has to say what to do instead ----------

@pytest.mark.skipif(session.IS_WINDOWS, reason="POSIX holder")
@pytest.mark.parametrize("code", [errno.ENOENT, errno.EACCES, errno.EROFS,
                                  errno.ENOSPC])
def test_a_holder_that_cannot_be_made_names_the_way_round_it(
        tmp_path, monkeypatch, code):
    """glibc keeps shm objects in /dev/shm and fails ENOENT where it is not
    mounted (chroots, some CI and build containers); a container's can be
    read-only or full, and a sandbox can refuse it. The bare "[Errno 2] No
    such file or directory" left nobody knowing what to do."""
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))

    def refuse(name, flags, mode=None):
        if flags & os.O_CREAT:
            raise OSError(code, os.strerror(code), name)
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    monkeypatch.setattr(session, "_shm_open", refuse)
    with pytest.raises(session.CryptoError) as err:
        session.store(str(tmp_path / "v.vault"), b"k" * 32)
    msg = str(err.value)
    assert "/dev/shm" in msg
    assert "COMPARTMENT_PASSPHRASE" in msg and "--once" in msg


@pytest.mark.skipif(session.IS_WINDOWS, reason="POSIX holder")
def test_openbsd_does_not_keep_the_secret_in_a_file_on_disk(
        tmp_path, monkeypatch):
    """OpenBSD's libc implements shm_open as /tmp/<sha256>.shm, an ordinary
    file on disk (lib/libc/gen/shm_open.c), so the secret would travel with
    any copy of the disk the session file is on. Storing must refuse there,
    the way it does where shm_open is missing, and say what to use."""
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))
    monkeypatch.setattr(session, "_libc_cache", None)
    monkeypatch.setattr(session.sys, "platform", "openbsd7")
    with pytest.raises(session.CryptoError) as err:
        session.store(str(tmp_path / "v.vault"), b"k" * 32)
    assert "OpenBSD" in str(err.value)
    assert "COMPARTMENT_PASSPHRASE" in str(err.value)
    assert not session.credential_path(str(tmp_path / "v.vault")).exists()
