"""Telling that another process rewrote the vault, on any filesystem.

A handle knows the vault changed under it by comparing what it read with
what is on disk now. Inode, size and modification time miss a rewrite on
the disks people carry vaults on: FAT and exFAT keep times to two seconds or
ten milliseconds and derive inode numbers from where a file landed, which a
later save reuses; a compaction often leaves the same size, since the
payload is whole SQLite pages; and a clock stepped back stamps a newer file
with an older time anywhere. A missed rewrite is a lost update: this
handle's next save writes its stale image over the other process's work.
"""
import os

import pytest

from compartment import vault as vault_mod, vaultfile
from compartment.vault import Vault, VaultStaleError

from conftest import PASS


class _With:
    def __init__(self, module, **replaced):
        self._module = module
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(self._module, name)


def _fat_like_stat(monkeypatch):
    """Every stat of the vault reports one inode number and one time, as a
    FAT stick can for two writes landing in the same slot within its clock
    tick. The size stays true."""
    def fake(result):
        fields = list(result)
        fields[1] = 4242                       # st_ino
        fields[8] = 1_700_000_000              # st_mtime
        st = os.stat_result(fields, {"st_mtime_ns": 1_700_000_000 * 10**9})
        return st

    def stat(path, *args, **kwargs):
        return fake(os.stat(path, *args, **kwargs))

    def fstat(fd):
        return fake(os.fstat(fd))

    for module in (vaultfile, vault_mod):
        monkeypatch.setattr(module, "os", _With(os, stat=stat, fstat=fstat))


def test_a_rewrite_the_stat_cannot_see_is_still_seen(vault_path, monkeypatch):
    first = Vault.create(vault_path, PASS, creator="t")
    rid = first.store("The build server is called ostrich.", caller="t",
                      tags=["aaaa"])["id"]
    first.lock()
    _fat_like_stat(monkeypatch)

    writer = Vault.unlock(vault_path, passphrase=PASS)
    stale = Vault.unlock(vault_path, passphrase=PASS)
    size = os.path.getsize(vault_path)
    writer.db.set_tags(rid, ["bbbb"])          # same length: same pages
    writer.save()
    assert os.path.getsize(vault_path) == size, \
        "the premise: a rewrite the same size as what it replaced"

    assert stale.is_stale(), "the other handle's save is noticed"
    with pytest.raises(VaultStaleError):
        stale.save()
    assert Vault.unlock(vault_path, passphrase=PASS).get(
        rid, caller="t")["tags"] == ["bbbb"], "and its change survives"


def test_an_untouched_vault_is_not_called_stale(vault_path):
    v = Vault.create(vault_path, PASS, creator="t")
    v.store("A memory nobody else touches.", caller="t")
    assert not v.is_stale()
    again = Vault.unlock(vault_path, passphrase=PASS)
    assert not again.is_stale()
    again.store("Another, from the same handle.", caller="t")
    assert not again.is_stale(), "its own writes are not someone else's"


def test_the_identity_is_the_header_and_the_payload_nonce(vault_path):
    """What changes on every save and on nothing else: an append keeps it,
    a save replaces it."""
    v = Vault.create(vault_path, PASS, creator="t")
    before = vaultfile.disk_state(vault_path)[3]
    v.store("Appended to the journal only.", caller="t")
    assert vaultfile.disk_state(vault_path)[3] == before
    v.save()
    assert vaultfile.disk_state(vault_path)[3] != before
    assert vaultfile.read_vault_file(vault_path).disk_state == \
        vaultfile.disk_state(vault_path)


def test_a_file_that_is_not_a_vault_still_has_a_state(tmp_path):
    p = tmp_path / "junk.vault"
    p.write_bytes(b"NUC")
    assert vaultfile.disk_state(str(p))[1] == 3
    p.write_bytes(b"NUCV\x00\x01\xff\xff\xff\xff" + b"x" * 100)
    assert vaultfile.disk_state(str(p))[1] == 110, "a garbage length is capped"
