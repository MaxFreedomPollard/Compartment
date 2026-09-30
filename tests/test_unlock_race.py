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
