import os
import signal
import subprocess
import sys
import textwrap

import pytest

from engram import audit
from engram.crypto import CryptoError
from engram.vault import Vault, VaultLockedError

from conftest import PASS, SRC


def test_store_search_get_forget(vault):
    r = vault.store("The launch code binder is in the safe", caller="test",
                    tags=["ops"])
    assert not r["duplicate"]
    hit = vault.search("where is the launch code binder", caller="test")["results"][0]
    assert hit["id"] == r["id"] and "safe" in hit["text"]
    got = vault.get(r["id"], caller="test")
    assert got["tags"] == ["ops"]
    vault.forget(r["id"], caller="test")
    assert vault.db.count() == 0


def test_duplicate_detection(vault):
    a = vault.store("Coffee restock happens every Monday", caller="test")
    b = vault.store("Coffee restock happens every Monday", caller="test")
    assert b["duplicate"] and b["id"] == a["id"]
    assert vault.db.count() == 1


def test_journal_replay_after_abandon(vault, vault_path):
    r = vault.store("must survive a crash", caller="test")
    del vault  # no lock/save - only header+journal on disk
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    assert v2.db.count() == 1
    assert "survive" in v2.get(r["id"], caller="test")["text"]
    ok, _, msg = audit.verify(v2.db.conn)
    assert ok, msg


def test_kill9_crash_recovery(tmp_path):
    """Real kill -9 mid-run: acknowledged writes survive, vault reopens clean."""
    vp = str(tmp_path / "crash.vault")
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(SRC)!r})
        from engram.vault import Vault
        v = Vault.create({vp!r}, {PASS!r})
        v.store("write one", caller="crash")
        v.store("write two", caller="crash")
        print("ACK", flush=True)
        os.kill(os.getpid(), 9)
    """)
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert "ACK" in p.stdout and p.returncode == -signal.SIGKILL
    v = Vault.unlock(vp, passphrase=PASS)
    assert v.db.count() == 2


def test_shred_removes_content_from_disk(vault, vault_path):
    needle = "XyzzyNeedle canary content"
    r = vault.store(f"secret: {needle}", caller="test")
    vault.save()
    vault.forget(r["id"], caller="test", shred=True)
    del vault
    raw = open(vault_path, "rb").read()
    assert needle.encode() not in raw  # (it's ciphertext anyway; belt+suspenders)
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    assert v2.db.count() == 0
    assert v2.search(needle, caller="test")["results"] == []


def test_locked_vault_refuses_everything(vault):
    vault.lock()
    with pytest.raises(VaultLockedError):
        vault.store("nope", caller="test")
    with pytest.raises(VaultLockedError):
        vault.search("nope", caller="test")
    with pytest.raises(VaultLockedError):
        vault.status()


def test_no_overwrite_on_create(vault, vault_path):
    vault.lock()
    with pytest.raises(CryptoError):
        Vault.create(vault_path, "whatever")


def test_export_import_roundtrip(vault, tmp_path):
    vault.store("alpha memory", caller="test", tags=["a"])
    vault.store("beta memory", caller="test", tags=["b"], quarantined=True)
    data = vault.export_jsonl()
    assert data.count("\n") == 2

    vp2 = str(tmp_path / "second.vault")
    v2 = Vault.create(vp2, PASS)
    assert v2.import_jsonl(data) == 2
    hit = v2.search("beta", caller="user")["results"][0]
    assert hit.get("quarantined") is True


def test_rekey(vault, vault_path):
    """rekey swaps in the USER'S new passphrase - nothing auto-generated,
    no recovery credential of any kind is created."""
    vault.store("survives rekey", caller="test")
    assert vault.rekey("NewHorse") is None
    with pytest.raises(CryptoError):
        Vault.unlock(vault_path, passphrase=PASS)  # old passphrase dead
    v2 = Vault.unlock(vault_path, passphrase="NewHorse")
    assert v2.db.count() == 1
    slot_types = [s["type"] for s in v2.header.keyslots]
    assert slot_types == ["passphrase"]        # no recovery slot generated


def test_signed_lock_and_verify(vault, vault_path):
    from nacl.signing import SigningKey
    from engram.vaultfile import read_vault_file, verify_manifest
    vault.store("sealed content", caller="test")
    vault.lock(signing_key=SigningKey.generate())
    m = verify_manifest(read_vault_file(vault_path))
    assert m["content_sha256"]
    # any post-signing modification breaks the seal
    raw = bytearray(open(vault_path, "rb").read())
    raw[-1] ^= 0xFF
    open(vault_path, "wb").write(bytes(raw))
    from engram.crypto import TamperError
    with pytest.raises(TamperError):
        verify_manifest(read_vault_file(vault_path))


def test_model_migration_reembed(vault, vault_path):
    vault.store("memory that must survive model migration", caller="test")
    # simulate a vault built with a different model
    vault.db.set_meta("model_sha256", "deadbeef" * 8)
    vault.header.model["sha256"] = "deadbeef" * 8
    vault.save()
    with pytest.raises(CryptoError):
        Vault.unlock(vault_path, passphrase=PASS)  # refuses (would corrupt search)
    v2 = Vault.unlock(vault_path, passphrase=PASS, check_model=False)
    n = v2.reembed()
    assert n == 1
    hit = v2.search("survive model migration", caller="test")["results"][0]
    assert "survive" in hit["text"]
    # and a plain unlock works again (model re-pinned)
    v3 = Vault.unlock(vault_path, passphrase=PASS)
    assert v3.db.count() == 1


def test_importance_boosts_ranking(vault):
    # Two memories similar to a query; the higher-importance one should win.
    vault.store("The project database is PostgreSQL", caller="test",
                importance=0.2, tags=["low"])
    vault.store("The project database is PostgreSQL", caller="test",
                importance=0.9, tags=["high"])  # not an exact dup: different tags
    # (exact-text dedup would merge these; make them distinct)
    vault.store("Our main datastore is a Postgres database on the ops server",
                caller="test", importance=0.9, tags=["high2"])
    hits = vault.search("what database do we use", caller="test", top_k=3)["results"]
    assert hits, "expected results"
    # the high-importance memory should not rank below the low one
    imps = [h["importance"] for h in hits]
    assert imps[0] >= 0.5
