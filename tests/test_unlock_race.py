"""A write made while another process is opening the vault must survive.

Vault.unlock reads the file, decrypts it, and only then built the Vault,
whose constructor took the file's size and modification time as "what this
copy holds". A memory another process appended in between was therefore
counted as read when it never was. Two things followed:

* the open's own compaction (it saves whenever it replayed journal entries)
  passed its staleness check and wrote the file back without that memory;
* an open that did not compact believed it was current, so its next write
  reused a journal position the other process had already taken, and the
  vault then failed its integrity check on every later open.

The status panel used to open the vault this way on every click, so a click
at the moment an agent stored something could lose it. Agents and the CLI
still open it this way. Now the state is taken as the file is read.
"""
import os
import struct
import zlib

import pytest

from compartment import vaultfile
from compartment.vault import Vault, VaultStaleError

from conftest import PASS


def _texts(v):
    return [r["text"] for r in v.recent("t", limit=20)["results"]]


def _append_during_the_open(monkeypatch, writer, text):
    """Make `writer` store `text` right after the opener has read and
    decrypted the file - the window that used to be invisible."""
    real = vaultfile.decrypt_payload
    fired = []

    def during(*a, **k):
        out = real(*a, **k)
        if not fired:
            fired.append(1)
            writer.store(text, caller="t", source="t")
        return out

    monkeypatch.setattr(vaultfile, "decrypt_payload", during)
    return fired


def test_a_memory_stored_during_an_open_is_not_lost(vault_path, monkeypatch):
    writer = Vault.create(vault_path, PASS, creator="t")
    # a journal entry, so the opener compacts - the save that overwrote
    writer.store("Memory one, journalled before the open.", caller="t",
                 source="t")
    fired = _append_during_the_open(monkeypatch, writer,
                                    "Memory two, stored during the open.")
    opener = Vault.unlock(vault_path, passphrase=PASS)
    assert fired
    monkeypatch.undo()
    assert any("Memory two" in t for t in _texts(opener))
    on_disk = _texts(Vault.unlock(vault_path, passphrase=PASS))
    assert any("Memory one" in t for t in on_disk)
    assert any("Memory two" in t for t in on_disk)


def test_an_open_that_raced_a_write_knows_it_is_behind(vault_path,
                                                       monkeypatch):
    writer = Vault.create(vault_path, PASS, creator="t")
    writer.save()                        # empty journal: the open won't save
    _append_during_the_open(monkeypatch, writer,
                            "Stored while the other process was opening.")
    opener = Vault.unlock(vault_path, passphrase=PASS)
    monkeypatch.undo()
    # It holds the file as it was read, and knows the file has moved on - so
    # an MCP server reloads before its next call instead of acting on it.
    assert opener.is_stale()
    # And a write from it is refused, not appended at a journal position the
    # other process already used - which left a vault that would not open.
    with pytest.raises(VaultStaleError):
        opener.store("A write from the copy that fell behind.", caller="t",
                     source="t")
    on_disk = _texts(Vault.unlock(vault_path, passphrase=PASS))
    assert any("Stored while the other process" in t for t in on_disk)


def _torn_append(path, nbytes):
    """The end of an append that died part way, `nbytes` long: a length
    prefix that checks out and less body than it declares. The next open
    discards it as a write that was never acknowledged."""
    length = struct.pack(">I", nbytes)
    head = length + struct.pack(">I", zlib.crc32(length) & 0xFFFFFFFF)
    with open(path, "ab") as f:
        f.write((head + b"\0" * nbytes)[:nbytes])


def test_a_compaction_of_the_same_length_in_the_same_tick_is_noticed(
        vault_path):
    """The staleness check compared size and modification time only.
    Another process's open compacts the journal into a new file; when that
    file had the old length and, on a coarse clock (ext4, NTFS), the old
    time, the writer still thought it was current and appended an entry
    sealed for its old journal position, and every later open failed with
    TamperError. Only the inode shows that the file is another one.

    Equal lengths happen on their own for about one journal length in
    4096, at lengths that depend on the SQLite build, so here the compacted
    file is brought to the old length with the torn end of an interrupted
    append, and the old time is given back with os.utime."""
    writer = Vault.create(vault_path, PASS, creator="t")
    writer.store("Memory one, journalled before the other process opened.",
                 caller="t", source="t")
    seen = os.stat(vault_path)
    Vault.unlock(vault_path, passphrase=PASS)      # replays and compacts
    gap = seen.st_size - os.path.getsize(vault_path)
    assert gap > 0, "precondition: the compaction left the file shorter"
    _torn_append(vault_path, gap)
    os.utime(vault_path, ns=(seen.st_atime_ns, seen.st_mtime_ns))
    now = os.stat(vault_path)
    assert (now.st_size, now.st_mtime_ns) == (seen.st_size, seen.st_mtime_ns)
    assert now.st_ino != seen.st_ino

    assert writer.is_stale()
    with pytest.raises(VaultStaleError):
        writer.store("A write from the copy that fell behind.", caller="t",
                     source="t")
    on_disk = _texts(Vault.unlock(vault_path, passphrase=PASS))
    assert any("Memory one" in t for t in on_disk)


def test_an_append_in_the_same_tick_is_still_noticed(vault_path):
    """The other half of the check: an append keeps the file's inode and,
    on a coarse clock, its time, so its size is what shows it."""
    writer = Vault.create(vault_path, PASS, creator="t")
    seen = os.stat(vault_path)
    other = Vault.unlock(vault_path, passphrase=PASS)   # nothing to compact
    other.store("Stored by the other process.", caller="t", source="t")
    os.utime(vault_path, ns=(seen.st_atime_ns, seen.st_mtime_ns))
    assert os.stat(vault_path).st_ino == seen.st_ino
    assert writer.is_stale()
