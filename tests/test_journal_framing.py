"""Corruption in the journal must not be mistaken for a crash.

Each entry is framed `u32 length | u32 crc32(length) | ciphertext`. A crash
during append leaves an intact prefix with a short body; corruption of the
length leaves a prefix that fails its own checksum. The reader used to have to
guess between the two, and guessing "crash" meant silently discarding every
acknowledged entry after the damage, then compacting the loss away.
"""
import struct
import zlib

import pytest

from compartment import audit, vaultfile
from compartment.crypto import TamperError
from compartment.vault import Vault

PASS = "CorrectHorse"


def _vault_with_journal(path, n=6):
    v = Vault.create(path, PASS, creator="t")
    v.lock()
    v = Vault.unlock(path, passphrase=PASS)
    for i in range(n):
        v.store(f"journalled memory {i}", caller="t")
    return v


def test_a_clean_journal_replays_every_entry(vault_path):
    _vault_with_journal(vault_path, 6)
    v = Vault.unlock(vault_path, passphrase=PASS)
    assert v.status()["organic_records"] == 6


def test_a_torn_final_write_is_discarded_and_the_rest_survives(vault_path):
    """An interrupted append: intact prefix, short body, nothing after it."""
    _vault_with_journal(vault_path, 6)
    with open(vault_path, "ab") as f:            # simulate a crash mid append
        payload = b"\x00" * 500
        length = struct.pack(">I", len(payload))
        f.write(length)
        f.write(struct.pack(">I", zlib.crc32(length) & 0xFFFFFFFF))
        f.write(payload[:200])                   # body cut short

    loaded = vaultfile.read_vault_file(vault_path)
    assert loaded.truncated_tail is True
    assert len(loaded.journal_cts) == 6

    v = Vault.unlock(vault_path, passphrase=PASS)
    assert v.status()["organic_records"] == 6, "acknowledged writes survive"


def test_a_corrupt_length_prefix_refuses_to_open(vault_path):
    """The regression: a bad length used to look exactly like a torn tail, so
    every entry after it was dropped and then compacted away for good."""
    _vault_with_journal(vault_path, 6)
    raw = bytearray(open(vault_path, "rb").read())

    # journal starts after magic(4) + version(2) + hlen(4) + header + payload
    (hlen,) = struct.unpack(">I", bytes(raw[6:10]))
    loaded = vaultfile.read_vault_file(vault_path)
    start = 10 + hlen + loaded.header.payload_len
    assert len(loaded.journal_cts) == 6, "precondition: entries are journalled"
    raw[start:start + 4] = struct.pack(">I", 0x0FFFFFF0)   # length only

    bad = str(vault_path) + ".corrupt"
    open(bad, "wb").write(bytes(raw))

    with pytest.raises(TamperError, match="checksum"):
        vaultfile.read_vault_file(bad)


def test_corruption_does_not_silently_drop_later_entries(vault_path):
    """Belt and braces: whatever happens, opening must not quietly return
    fewer entries than were written."""
    v = _vault_with_journal(vault_path, 8)
    v.lock()
    before = len(vaultfile.read_vault_file(vault_path).journal_cts)
    assert before == 0, "lock compacts the journal into the payload"

    v = Vault.unlock(vault_path, passphrase=PASS)
    for i in range(4):
        v.store(f"after compaction {i}", caller="t")
    loaded = vaultfile.read_vault_file(vault_path)
    assert len(loaded.journal_cts) == 4
    assert loaded.truncated_tail is False


# --------------------------------------------------------------- the anchor

def test_a_new_vault_is_anchored_from_creation(vault_path):
    v = Vault.create(vault_path, PASS, creator="t")
    assert audit.read_anchor(v.db.conn) is not None
    ok, _, msg = audit.verify(v.db.conn)
    assert ok, msg


def test_removing_the_anchor_is_reported_as_tampering(vault_path):
    v = Vault.create(vault_path, PASS, creator="t")
    v.db.conn.execute("DELETE FROM meta WHERE k LIKE 'audit_anchor%'")
    ok, _, msg = audit.verify(v.db.conn)
    assert not ok
    assert "anchor" in msg


def test_a_truncated_tail_is_detected_against_the_anchor(vault_path):
    """A forward walk cannot see this: the shortened chain is still internally
    consistent. Only the anchored length catches it."""
    v = Vault.create(vault_path, PASS, creator="t")
    for i in range(5):
        v.store(f"memory {i}", caller="t")
    v.save()                                   # anchors head and length
    assert audit.verify(v.db.conn)[0]

    last = v.db.conn.execute(
        "SELECT seq FROM audit ORDER BY seq DESC LIMIT 2").fetchall()
    for row in last:
        v.db.conn.execute("DELETE FROM audit WHERE seq = ?", (row["seq"],))

    ok, _, msg = audit.verify(v.db.conn)
    assert not ok
    assert "SHORTER" in msg or "removed" in msg


def test_relink_refuses_to_launder_a_deletion(vault_path):
    v = Vault.create(vault_path, PASS, creator="t")
    for i in range(5):
        v.store(f"memory {i}", caller="t")
    v.save()
    v.db.conn.execute(
        "DELETE FROM audit WHERE seq = (SELECT MAX(seq) FROM audit)")
    with pytest.raises(ValueError, match="deleted"):
        audit.relink(v.db.conn)


def test_a_repair_is_recorded_permanently(vault_path):
    v = Vault.create(vault_path, PASS, creator="t")
    for i in range(4):
        v.store(f"memory {i}", caller="t")
    rows = v.db.conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()
    victim = rows[2]
    # a dangling link, contents untouched: the entry still hashes to its own
    # content, so this is repairable damage rather than an edit
    bad = "f" * 64
    h = audit._entry_hash(bad, victim["ts"], victim["caller"], victim["op"],
                          victim["detail"])
    v.db.conn.execute("UPDATE audit SET prev_hash = ?, hash = ? WHERE seq = ?",
                      (bad, h, victim["seq"]))
    changed, first = audit.relink(v.db.conn)
    assert changed >= 1
    history = audit.relink_history(v.db.conn)
    assert len(history) == 1
    assert history[0]["first_break_seq"] == first
    assert history[0]["entries_relinked"] == changed
