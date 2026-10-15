import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from compartment import audit
from compartment.crypto import CryptoError
from compartment.vault import Vault, VaultLockedError, VaultStaleError

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
        from compartment.vault import Vault
        v = Vault.create({vp!r}, {PASS!r})
        v.store("write one", caller="crash")
        v.store("write two", caller="crash")
        print("ACK", flush=True)
        os.kill(os.getpid(), 9)
    """)
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    # Windows has no SIGKILL: os.kill(pid, 9) is TerminateProcess(exit code 9).
    killed_rc = 9 if os.name == "nt" else -signal.SIGKILL
    assert "ACK" in p.stdout and p.returncode == killed_rc
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
    from compartment.vaultfile import read_vault_file, verify_manifest
    vault.store("sealed content", caller="test")
    vault.lock(signing_key=SigningKey.generate())
    m = verify_manifest(read_vault_file(vault_path))
    assert m["content_sha256"]
    # any post-signing modification breaks the seal
    raw = bytearray(open(vault_path, "rb").read())
    raw[-1] ^= 0xFF
    open(vault_path, "wb").write(bytes(raw))
    from compartment.crypto import TamperError
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


def test_document_frequencies_do_not_go_stale_after_a_write(vault):
    """The lexical channel died silently mid-session. A term first asked for
    when nothing matched it cached at df 0, term_information then dropped it,
    and the whole literal-evidence half of ranking went quiet for that query -
    for a server holding one vault open, that is the rest of the session."""
    db = vault.db
    assert db.doc_frequency("airtable") == 0          # cold, caches the 0
    # Distinct enough that the near-duplicate guard keeps all three.
    for text in ("The Airtable base holds the pen name roster",
                 "Billing for Airtable renews in March",
                 "Airtable rate limits are five requests a second"):
        vault.store(text, caller="test")
    assert db.doc_frequency("airtable") == 3
    assert "Airtable" in db.term_information("Airtable roster")


def test_document_frequencies_are_refreshed_after_a_delete(vault):
    r = vault.store("a memory mentioning quokkas", caller="test")
    assert vault.db.doc_frequency("quokkas") == 1
    vault.forget(r["id"], caller="test")
    assert vault.db.doc_frequency("quokkas") == 0


def test_writing_into_a_deleted_vault_file_is_refused(vault, vault_path):
    """is_stale() ANDed in an existence test, so a missing file read as "not
    stale". _journal then appended with open(path, "ab"), recreating it as
    journal frames with no header: every later write reported success into a
    file nothing can ever open again."""
    vault.store("a first memory that should survive", caller="test")
    vault.save()
    os.unlink(vault_path)

    assert vault.is_stale() is True
    with pytest.raises(Exception) as exc:
        vault.store("written into a file that no longer exists", caller="test")
    assert "gone" in str(exc.value).lower()
    assert not os.path.exists(vault_path), \
        "the refused write recreated the file anyway"


def _assert_holds_no_plaintext(v):
    """A locked vault keeps neither the decrypted database, whose full-text
    table holds every memory in plain text, nor the vectors and the index."""
    with pytest.raises(sqlite3.ProgrammingError):
        v.db.conn.execute("SELECT text FROM fts")
    assert v.index is None and v._id_by_ikey == {}


def test_a_locked_vault_holds_no_decrypted_memories(vault, vault_path):
    """lock() dropped the key and nothing else: the in-RAM database stayed
    open with every memory's text in it, readable without any key, for as
    long as the object lived. In the server that is the whole idle period
    auto-lock exists for."""
    r = vault.store("The VPN password hint is the dog's name", caller="test")
    vault.search("VPN password hint", caller="test")    # audited in RAM only
    vault.lock()
    _assert_holds_no_plaintext(vault)
    with pytest.raises(VaultLockedError):
        vault.get(r["id"], caller="test")
    # The lock still saved before it let go: the search's audit row is on disk.
    again = Vault.unlock(vault_path, passphrase=PASS)
    ops = [row["op"] for row in again.db.conn.execute(
        "SELECT op FROM audit ORDER BY seq")]
    assert ops[-1] == "search"


def test_a_lock_whose_save_fails_still_locks(vault, vault_path):
    """lock() saved first and dropped the key only if the save worked. Once
    another process had written the file, every save raised VaultStaleError,
    so the server's auto-lock retried on the same handle every 30 seconds
    and the key stayed in RAM for as long as the server stayed idle."""
    vault.store("stored before the server went idle", caller="test")
    other = Vault.unlock(vault_path, raw_key=vault._master)
    other.store("stored by another process meanwhile", caller="test")
    with pytest.raises(VaultStaleError):
        vault.lock()
    assert vault._locked and vault._master is None
    _assert_holds_no_plaintext(vault)
    with pytest.raises(VaultLockedError):
        vault.search("stored", caller="test")


class _ContendedLock:
    """A vault's operation lock that says when a second thread had to wait
    for it, which is how the tests below order two threads without sleeping."""

    def __init__(self, contended: threading.Event):
        self._lock = threading.RLock()
        self._contended = contended

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


def test_get_finishes_before_a_lock_takes_the_key(vault, monkeypatch):
    """get() was the one public operation outside the vault's lock. The
    server's auto-lock could drop the key between get() reading the row and
    decrypting it, and the decrypt with no key was reported as tampering."""
    rid = vault.store("The spare key is under the blue flowerpot",
                      caller="test")["id"]
    go = threading.Event()             # the lock is waiting, or has finished
    vault._oplock = _ContendedLock(go)
    row_read = threading.Event()
    real_get_row = vault.db.get_row

    def get_row(record_id):
        row = real_get_row(record_id)
        if threading.current_thread().name == "getter":
            row_read.set()
            go.wait(10)
        return row
    monkeypatch.setattr(vault.db, "get_row", get_row)
    out = {}

    def getter():
        try:
            out["text"] = vault.get(rid, caller="test")["text"]
        except Exception as exc:                        # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
    t = threading.Thread(target=getter, name="getter")
    t.start()
    assert row_read.wait(10)
    vault.lock()
    go.set()
    t.join(10)
    assert "flowerpot" in out.get("text", ""), out


def test_get_and_store_on_two_threads_keep_one_audit_chain(vault, monkeypatch):
    """get() appended its audit row without the vault's lock, and an append
    reads the head before it inserts. A store on another thread in between
    linked to the same head: a forked chain, saved into the vault file, that
    every later verify reports as a break."""
    rid = vault.store("The boiler was serviced in May", caller="test")["id"]
    go = threading.Event()             # the store is waiting, or has finished
    vault._oplock = _ContendedLock(go)
    head_read = threading.Event()
    real_entry_hash = audit._entry_hash

    def entry_hash(prev, ts, caller, op, detail):
        if threading.current_thread().name == "getter" and op == "get":
            head_read.set()            # get() holds the head it will link to
            go.wait(10)
        return real_entry_hash(prev, ts, caller, op, detail)
    monkeypatch.setattr(audit, "_entry_hash", entry_hash)
    t = threading.Thread(target=lambda: vault.get(rid, caller="test"),
                         name="getter")
    t.start()
    assert head_read.wait(10)
    vault.store("The chimney was swept in June", caller="test")
    go.set()
    t.join(10)
    ok, _, msg = audit.verify(vault.db.conn)
    assert ok, msg
