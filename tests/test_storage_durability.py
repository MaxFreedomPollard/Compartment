"""What reaches the disk, and what a failed write leaves behind.

The journal's promise is that an acknowledged write survives and an
unacknowledged one costs nothing. These tests hold the second half to the
disk running out part way through, which used to leave a torn tail that no
open could get past while the disk stayed full.
"""
import errno
import os

import pytest

from compartment import vaultfile
from compartment.vault import Vault

from conftest import PASS


class _With:
    """A module as one other module sees it, with some functions replaced."""

    def __init__(self, module, **replaced):
        self._module = module
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(self._module, name)


class _Budget:
    """A disk with `room` bytes left: writes past it land in part and then
    fail with ENOSPC, the way a real full disk answers."""

    def __init__(self, room: int):
        self.room = room

    def take(self, data) -> int:
        n = min(len(data), self.room)
        self.room -= n
        return n


def _disk_fills_during_appends(monkeypatch, budget: _Budget):
    """Both ways an append can reach the file - os.write on a descriptor and
    a Python file object - run out of room together."""
    real_write = os.write
    real_open = open

    def write(fd, data):
        n = budget.take(data)
        if n == 0 and len(data):
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(fd, bytes(data[:n]))
        if n < len(data):
            raise OSError(errno.ENOSPC, "No space left on device")
        return n

    class _File:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            n = budget.take(data)
            self._f.write(data[:n])
            if n < len(data):
                self._f.flush()
                raise OSError(errno.ENOSPC, "No space left on device")
            return n

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()

    def opener(path, mode="r", *args, **kwargs):
        f = real_open(path, mode, *args, **kwargs)
        return _File(f) if "a" in mode else f

    monkeypatch.setattr(vaultfile, "os", _With(os, write=write))
    monkeypatch.setattr(vaultfile, "open", opener, raising=False)


def _disk_stays_full(monkeypatch):
    """No save can be written. Patched where the vault calls it, never as
    `_atomic_replace`: modules imported for the first time during the test
    bind that name for good, and would carry the full disk into the next."""
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(vaultfile, "write_vault_file", full)


def test_an_append_the_disk_refuses_leaves_no_torn_tail(vault_path,
                                                        monkeypatch):
    """The partial entry stayed on the end of the file. Nothing had been
    acknowledged, but every open after it had to compact the tail away, and
    on a disk that was still full that compaction failed and the open with
    it: every agent's server, `compartment status` and the panel lost the
    vault, reads included, until someone freed space."""
    v = Vault.create(vault_path, PASS, creator="t")
    v.store("The first memory reached the disk whole.", caller="t")
    size = os.path.getsize(vault_path)

    with monkeypatch.context() as m:
        _disk_fills_during_appends(m, _Budget(room=40))
        with pytest.raises(OSError, match="No space"):
            v.store("The second memory ran out of disk part way.", caller="t")

    assert os.path.getsize(vault_path) == size, \
        "the refused bytes are taken back off the end"
    loaded = vaultfile.read_vault_file(vault_path)
    assert not loaded.truncated_tail

    _disk_stays_full(monkeypatch)
    again = Vault.unlock(vault_path, passphrase=PASS)
    texts = [r["text"] for r in again.peek_recent("t")["results"]]
    assert len(texts) == 1
    assert texts[0].startswith("The first memory reached the disk whole.")


def test_an_append_whose_sync_fails_is_not_left_to_replay(vault_path,
                                                          monkeypatch):
    """Every byte written, then the sync refused (EIO, a full quota on a
    network share). The caller was told the store failed, so the entry must
    not come back on the next open as a memory nobody stored."""
    v = Vault.create(vault_path, PASS, creator="t")
    v.store("A memory that was acknowledged.", caller="t")
    size = os.path.getsize(vault_path)
    calls = []
    real_sync = vaultfile._sync

    def sync(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(errno.EIO, "Input/output error")
        return real_sync(fd)

    with monkeypatch.context() as m:
        m.setattr(vaultfile, "_sync", sync)
        with pytest.raises(OSError, match="Input/output"):
            v.store("A memory whose sync failed.", caller="t")
    assert os.path.getsize(vault_path) == size
    assert Vault.unlock(vault_path, passphrase=PASS).status()[
        "organic_records"] == 1


def test_an_append_never_creates_a_vault_that_is_not_there(vault_path):
    """An append to a missing file used to create it, holding journal
    frames and no header: a file no build can open. The writer's own
    staleness check refuses first; this is the floor under it."""
    v = Vault.create(vault_path, PASS, creator="t")
    os.unlink(vault_path)
    with pytest.raises(FileNotFoundError):
        vaultfile.append_journal_entry(vault_path, v.header, 0,
                                       {"op": "noop"}, v._master)
    assert not os.path.exists(vault_path)


class _Fcntl:
    """fcntl as the vault file sees it: records every call, and refuses
    them all when `refuse` is set, as SMB and some USB media do."""

    def __init__(self, refuse: bool = False):
        self.calls, self.refuse = [], refuse

    def fcntl(self, fd, op, *args):
        self.calls.append(op)
        if self.refuse:
            raise OSError(errno.ENOTSUP, "Operation not supported")
        return 0


def _record_fsyncs(monkeypatch) -> list:
    fsyncs = []

    def fsync(fd):
        fsyncs.append(fd)
        return os.fsync(fd)
    monkeypatch.setattr(vaultfile, "os", _With(os, fsync=fsync))
    return fsyncs


def test_appends_and_compactions_reach_the_disk_on_macos(vault_path,
                                                          monkeypatch):
    """fsync on macOS hands data to the drive's own cache and no further.
    An acknowledged store could be lost to a power cut, and a journal entry
    whose length landed before its body read back as tampering, so the
    vault refused to open. F_FULLFSYNC is the call Apple documents for it."""
    v = Vault.create(vault_path, PASS, creator="t")
    fake = _Fcntl()
    monkeypatch.setattr(vaultfile, "fcntl", fake)
    monkeypatch.setattr(vaultfile, "_FULLFSYNC", 51)    # macOS's value
    fsyncs = _record_fsyncs(monkeypatch)
    v.store("A memory that has to survive a power cut.", caller="t")
    assert fake.calls == [51], "the append is flushed past the drive cache"
    v.save()
    assert fake.calls[1] == 51, "and so is the compaction's copy"
    assert not fsyncs, "F_FULLFSYNC does fsync's work and more"


def test_a_disk_that_refuses_a_full_sync_still_gets_fsync(vault_path,
                                                          monkeypatch):
    """A vault on an SMB share or a USB stick, where the filesystem does not
    implement F_FULLFSYNC: a store must not fail for want of it."""
    v = Vault.create(vault_path, PASS, creator="t")
    monkeypatch.setattr(vaultfile, "fcntl", _Fcntl(refuse=True))
    monkeypatch.setattr(vaultfile, "_FULLFSYNC", 51)
    fsyncs = _record_fsyncs(monkeypatch)
    v.store("A memory on a share without F_FULLFSYNC.", caller="t")
    assert len(fsyncs) == 1
    assert Vault.unlock(vault_path, passphrase=PASS).status()[
        "organic_records"] == 1


def test_elsewhere_an_append_is_an_ordinary_fsync(vault_path, monkeypatch):
    v = Vault.create(vault_path, PASS, creator="t")
    monkeypatch.setattr(vaultfile, "_FULLFSYNC", None)
    fsyncs = _record_fsyncs(monkeypatch)
    v.store("A memory on Linux or Windows.", caller="t")
    assert len(fsyncs) == 1


def _record_save(monkeypatch, name: str = "posix") -> list:
    """What a save does to the disk, in order: syncs (with what they
    synced), the rename, and every folder opened."""
    order = []
    real_sync, real_open = vaultfile._sync, os.open

    def sync(fd):
        order.append(("sync", None))
        return real_sync(fd)

    def replace(src, dst):
        order.append(("replace", None))
        return os.replace(src, dst)

    def opener(path, flags, *args):
        if os.path.isdir(path):
            order.append(("open folder", path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(vaultfile, "_sync", sync)
    monkeypatch.setattr(vaultfile, "os", _With(os, name=name, replace=replace,
                                               open=opener))
    return order


@pytest.mark.skipif(os.name == "nt", reason="a folder cannot be opened "
                    "for syncing on Windows; the test below covers it")
def test_the_rename_of_a_compaction_is_synced_through_its_folder(
        vault_path, monkeypatch):
    """Syncing the file makes its bytes durable, not the name pointing at
    them. After a power cut the old vault could come back, and every store
    acknowledged since the compaction - each appended to the NEW file -
    went with it."""
    v = Vault.create(vault_path, PASS, creator="t")
    v.store("A memory that the compaction folds in.", caller="t")
    order = _record_save(monkeypatch)
    v.save()
    kinds = [k for k, _ in order]
    assert kinds == ["sync", "replace", "open folder", "sync"]
    assert order[2][1] == os.path.dirname(os.path.abspath(vault_path))


def test_on_windows_no_folder_is_opened(vault_path, monkeypatch):
    """A directory cannot be opened for syncing on Windows, and NTFS
    journals the rename itself."""
    v = Vault.create(vault_path, PASS, creator="t")
    order = _record_save(monkeypatch, name="nt")
    v.save()
    assert [k for k, _ in order] == ["sync", "replace"]


def test_a_folder_that_cannot_be_synced_does_not_fail_the_save(
        vault_path, monkeypatch):
    """Some FUSE and network mounts refuse to sync a directory. The save
    has already succeeded by then and must say so."""
    v = Vault.create(vault_path, PASS, creator="t")
    v.store("A memory on a mount that cannot sync folders.", caller="t")
    real_sync, real_replace = vaultfile._sync, os.replace
    renamed = []

    def sync(fd):
        if renamed:                     # the folder's sync, after the rename
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_sync(fd)

    def replace(src, dst):
        renamed.append(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(vaultfile, "_sync", sync)
    monkeypatch.setattr(vaultfile, "os", _With(os, replace=replace))
    v.save()
    assert Vault.unlock(vault_path, passphrase=PASS).status()[
        "organic_records"] == 1


def test_a_store_whose_journal_did_not_land_cannot_be_saved_later(
        vault_path, monkeypatch):
    """The store changes RAM before it journals. When the journal never
    landed - here the write lock stayed busy past its timeout, as behind a
    long import in another process - the record stayed in RAM: a retry of
    the same store was answered "duplicate" from it and journalled nothing,
    and the memory the caller believed stored was gone at the next reopen,
    or a later save wrote the one the caller was told had failed."""
    from compartment import vault as vault_mod
    from compartment.crypto import CryptoError
    from compartment.vault import VaultStaleError

    v = Vault.create(vault_path, PASS, creator="t")
    real_lock = vault_mod.FileLock
    calls = []

    def busy_once(path, timeout=10.0):
        calls.append(path)
        if len(calls) == 1:
            raise CryptoError("Vault is busy: another process holds the "
                              "write lock (waited 10s)")
        return real_lock(path, timeout=timeout)

    monkeypatch.setattr(vault_mod, "FileLock", busy_once)
    text = "The parking spot is level 3, bay 41."
    with pytest.raises(CryptoError, match="busy"):
        v.store(text, caller="t")
    assert v.is_stale(), "the handle no longer matches the file"
    with pytest.raises(VaultStaleError, match="did not reach"):
        v.store(text, caller="t")
    with pytest.raises(VaultStaleError):
        v.save()

    again = Vault.unlock(vault_path, passphrase=PASS)   # what the server does
    assert again.db.count() == 0
    out = again.store(text, caller="t")
    assert not out["duplicate"]
    assert Vault.unlock(vault_path, passphrase=PASS).db.count() == 1


def test_a_refused_append_that_wrote_nothing_is_not_saved_later(
        vault_path, monkeypatch):
    """A disk already full refuses the first byte: the file is untouched,
    so nothing about it says this handle is behind - but its RAM holds the
    record, and the next save wrote it."""
    from compartment.vault import VaultStaleError

    v = Vault.create(vault_path, PASS, creator="t")
    with monkeypatch.context() as m:
        _disk_fills_during_appends(m, _Budget(room=0))
        with pytest.raises(OSError, match="No space"):
            v.store("A memory refused at the first byte.", caller="t")
    with pytest.raises(VaultStaleError):
        v.save()
    assert Vault.unlock(vault_path, passphrase=PASS).db.count() == 0
