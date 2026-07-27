"""The audit chain has to survive ordinary multi-process use.

It did not. A write is journalled the moment it happens; a read is audited in
RAM and only reaches disk on the next save. So a `store` would chain off a
`search` entry that no other process had, its journal entry was replayed
verbatim into a log missing that entry, and the link dangled. verify() stopped
there and never reported on anything after it again.

That is not a hypothetical either: it happened five times in one day on a real
vault, every one of them a `store` from a long-running MCP server.
"""
import pytest

from compartment import audit, store, vaultfile
from compartment.vault import Vault

from conftest import PASS


def _audit_rows(conn):
    return conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()


def test_replaying_a_journalled_write_keeps_the_chain_linked(vault_path):
    """The regression. A read between saves used to poison the next write."""
    v = Vault.create(vault_path, PASS, creator="test")
    v.save()

    # a read: audited in RAM, deliberately not journalled and not saved
    v.search("anything", caller="test")
    # a write: journalled immediately, chaining off that RAM-only entry
    v.store("a memory written after a search", caller="test")
    # crash before save, so the payload never learns about either entry
    del v

    reopened = Vault.unlock(vault_path, passphrase=PASS)
    ok, n, msg = audit.verify(reopened.db.conn)
    assert ok, msg
    texts = {reopened.db.decrypt_text(r, reopened._master)
             for r in reopened.db.conn.execute("SELECT * FROM records")}
    assert "a memory written after a search" in texts


def test_replay_preserves_when_the_operation_happened(vault_path):
    v = Vault.create(vault_path, PASS, creator="test")
    v.save()
    v.store("timed", caller="test")
    stored_at = _audit_rows(v.db.conn)[-1]["ts"]
    del v

    reopened = Vault.unlock(vault_path, passphrase=PASS)
    replayed = [r for r in _audit_rows(reopened.db.conn) if r["op"] == "store"][-1]
    assert replayed["ts"] == stored_at, "replay must not restamp history"


def test_append_links_to_the_head_it_can_see(vault_path):
    v = Vault.create(vault_path, PASS, creator="test")
    conn = v.db.conn
    audit.append(conn, "a", "op1", "one")
    head = audit.head(conn)
    audit.append(conn, "b", "op2", "two", ts=123.0)
    last = _audit_rows(conn)[-1]
    assert last["prev_hash"] == head
    assert last["ts"] == 123.0
    ok, _, msg = audit.verify(conn)
    assert ok, msg


# ---------------------------------------------------------------------------
# Repairing a log an older build already broke
# ---------------------------------------------------------------------------

def _break_the_chain(conn):
    """Exactly the old damage: a dangling prev_hash, contents untouched."""
    rows = _audit_rows(conn)
    victim = rows[len(rows) // 2]
    bad = "f" * 64
    h = audit._entry_hash(bad, victim["ts"], victim["caller"], victim["op"],
                          victim["detail"])
    conn.execute("UPDATE audit SET prev_hash = ?, hash = ? WHERE seq = ?",
                 (bad, h, victim["seq"]))
    return victim["seq"]


def test_relink_repairs_a_dangling_link_and_keeps_every_entry(vault):
    conn = vault.db.conn
    for i in range(6):
        audit.append(conn, "test", "store", f"entry {i}")
    before = [(r["ts"], r["caller"], r["op"], r["detail"]) for r in _audit_rows(conn)]

    broken_at = _break_the_chain(conn)
    assert not audit.verify(conn)[0]

    changed, first = audit.relink(conn)
    assert first == broken_at
    assert changed >= 1
    ok, n, msg = audit.verify(conn)
    assert ok, msg
    assert n == len(before)

    after = [(r["ts"], r["caller"], r["op"], r["detail"]) for r in _audit_rows(conn)]
    assert after == before, "relink must not alter what the log says"


def test_relink_is_a_no_op_on_an_intact_chain(vault):
    conn = vault.db.conn
    audit.append(conn, "test", "store", "x")
    assert audit.relink(conn) == (0, None)
    assert audit.verify(conn)[0]


def test_relink_refuses_to_hide_edited_content(vault):
    """The distinction that matters: a dangling link is damage, a reworded
    entry is tampering, and relinking the second would erase the proof."""
    conn = vault.db.conn
    audit.append(conn, "test", "forget", "id=secret-thing")
    rows = _audit_rows(conn)
    conn.execute("UPDATE audit SET detail = ? WHERE seq = ?",
                 ("id=something-harmless", rows[-1]["seq"]))

    with pytest.raises(ValueError, match="content tampering"):
        audit.relink(conn)
    assert not audit.verify(conn)[0], "and it stays broken, as it should"
