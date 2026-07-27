"""The on-disk format is not branding, and must survive every rename.

Compartment was called engRAM until 1.15.0, and the name was still in the
bytes until 2.2: AEAD associated data, KDF context labels, credential
identifiers. Changing one of those does not rename anything. It makes data
already written under the old spelling undecryptable.

`engram-keyslot` is the worst of them: it is the associated data wrapping the
master key. Lose it and no keyslot unseals, which means the vault cannot be
opened again by anyone, with any passphrase, ever. `engram-record:`,
`engram-record-body:` and `engram-payload:` are the same hazard one layer down.

This is not hypothetical. The 1.15.0 rename did a find-and-replace across the
source and rewrote all sixteen of them. It surfaced only because the vault
started reporting itself locked; had the session not broken first, the next
person to run it against a real vault would have found their data unreadable
and no error explaining why.

2.2 renamed them deliberately, which is a different thing: every old spelling
moved into a legacy tuple in wire.py and is still tried on read. So the rule
is no longer "these constants are frozen". It is:

    every spelling ever written stays readable, forever.

These tests hold that line from both ends. The literal checks catch a legacy
value being deleted or a find-and-replace sweeping through wire.py. The
round-trip tests are the ones that matter: they build genuine pre-2.2 bytes
and prove the current code still opens them.
"""
import pathlib

import pytest

from compartment import crypto, store, vaultfile, wire

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "compartment"

# Every legacy spelling, and the wire.py attribute it has to stay in. Deleting
# any one of these entries is the permanent-data-loss move.
LEGACY = [
    ("KEYSLOT_LEGACY", b"engram-keyslot"),
    ("KEYFILE_SEP_LEGACY", b"\x1f engram-2fa \x1f"),
    ("_PAYLOAD_LEGACY", b"engram-payload:"),
    ("_JOURNAL_LEGACY", b"engram-journal:"),
    ("_RECORD_KEY_LEGACY", b"engram-record:"),
    ("_RECORD_BODY_LEGACY", b"engram-record-body:"),
    ("_PACK_LEGACY", b"engram-pack:"),
    ("_SESSION_LEGACY", b"engram-session:"),
    ("SESSION_TOKEN_LEGACY", "engram-session-v2"),
    ("KEYCHAIN_SERVICE_LEGACY", "engram-vault:"),
    ("KEYCHAIN_ACCOUNT_LEGACY", "engram"),
]


@pytest.mark.parametrize("attr,value", LEGACY)
def test_every_legacy_spelling_is_still_declared(attr, value):
    assert value in getattr(wire, attr), (
        f"wire.{attr} no longer lists {value!r}. That is not a name, it is "
        f"associated data in bytes that are already on disk. Removing it "
        f"makes every vault still carrying it undecryptable, permanently."
    )


def test_wire_is_the_only_place_the_labels_live():
    """One file to check before the next rename.

    The 1.15.0 accident happened because these constants were scattered across
    six modules, so a find-and-replace hit them all and no single file looked
    wrong afterwards."""
    strays = []
    for path in sorted(SRC.glob("*.py")):
        if path.name in ("wire.py", "home.py"):
            continue          # wire.py owns them; home.py handles ~/.engram
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "engram" in line:
                strays.append(f"{path.name}:{n}: {line.strip()}")
    assert not strays, (
        "on-disk labels must be declared in wire.py, not inline:\n  "
        + "\n  ".join(strays))


# ---------------------------------------------------------------------------
# The real test: build pre-2.2 bytes, open them with current code.
# ---------------------------------------------------------------------------

@pytest.fixture()
def legacy_wire(monkeypatch):
    """Make the writing side emit exactly what a pre-2.2 build emitted."""
    for attr, value in LEGACY:
        monkeypatch.setattr(wire, attr.removesuffix("_LEGACY"), value)
    monkeypatch.setattr(wire, "WIRE_FORMAT", 1)
    return wire


def test_a_legacy_keyslot_still_opens_and_migrates_itself(legacy_wire, monkeypatch):
    master = crypto.new_key()
    slot = crypto.make_passphrase_slot(master, "correct horse")
    assert b"engram-keyslot" == wire.KEYSLOT          # the fixture is honest
    monkeypatch.undo()                                 # back to current code

    assert crypto.open_slot(slot, "correct horse") == master
    # and it rewrote itself on the way through, so the next open is direct
    assert crypto.unseal(
        crypto.derive_key(b"correct horse", bytes.fromhex(slot["salt"]),
                          slot["kdf"]),
        bytes.fromhex(slot["wrapped"]), aad=wire.KEYSLOT) == master


def test_a_legacy_record_still_reads_and_migrates(legacy_wire, monkeypatch):
    master = crypto.new_key()
    db = store.Store()
    rid = db.insert(record_id=None, ns="main", text="a legacy memory",
                    vec=_vec(), tags=[], importance=0.5, quarantined=False,
                    pack=None, prov={}, master_key=master)
    monkeypatch.undo()

    assert db.decrypt_text(db.get_row(rid), master) == "a legacy memory"
    assert db.migrate_wire(master) == 1
    row = db.get_row(rid)
    assert db.decrypt_text(row, master) == "a legacy memory"
    # now genuinely current: the legacy label is no longer what opens it
    rk = crypto.unseal(master, row["key_wrapped"], aad=wire.record_key(rid)[0])
    assert crypto.unseal(rk, row["ct"], aad=wire.record_body(rid)[0])


def test_a_legacy_vault_opens_migrates_and_keeps_every_memory(
        legacy_wire, monkeypatch, vault_path):
    """End to end, through the real Vault, on a real file."""
    from compartment.vault import Vault

    v = Vault.create(vault_path, "CorrectHorse", creator="test")
    for text in ("the first memory", "the second memory", "the third"):
        v.store(text, caller="test")
    v.save()
    v.lock()
    assert v.header.extra["wire"] == 1
    monkeypatch.undo()

    reopened = Vault.unlock(vault_path, passphrase="CorrectHorse")
    assert reopened.header.extra["wire"] == wire.WIRE_FORMAT
    texts = {reopened.db.decrypt_text(r, reopened._master)
             for r in reopened.db.conn.execute("SELECT * FROM records")}
    assert {"the first memory", "the second memory", "the third"} <= texts

    # The migration persisted, and it is genuinely current rather than just
    # marked so: every layer now authenticates under the current label with no
    # fallback. Checking the file for "engram" would prove nothing - associated
    # data is an input to the AEAD, never stored, so a legacy vault does not
    # contain the string either.
    reopened.lock()
    again = Vault.unlock(vault_path, passphrase="CorrectHorse")
    assert again.header.extra["wire"] == wire.WIRE_FORMAT

    loaded = vaultfile.read_vault_file(vault_path)
    key = again._master
    crypto.unseal(key, loaded.payload_ct,
                  aad=wire.payload(loaded.header.vault_id)[0])
    for row in again.db.conn.execute("SELECT * FROM records"):
        rk = crypto.unseal(key, row["key_wrapped"],
                           aad=wire.record_key(row["id"])[0])
        crypto.unseal(rk, row["ct"], aad=wire.record_body(row["id"])[0])
    slot = again.header.keyslots[0]
    crypto.unseal(
        crypto.derive_key(b"CorrectHorse", bytes.fromhex(slot["salt"]),
                          slot["kdf"]),
        bytes.fromhex(slot["wrapped"]), aad=wire.KEYSLOT)


def test_a_legacy_session_credential_is_accepted_and_rewritten(
        legacy_wire, monkeypatch, tmp_path):
    from compartment import session

    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "session"))
    master = crypto.new_key()
    vault = str(tmp_path / "x.vault")
    session.store(vault, master)
    monkeypatch.undo()
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "session"))

    assert session.get(vault) == master          # opened despite the old token
    assert session.get(vault) == master          # and again, now rewritten


def test_a_legacy_encrypted_pack_still_opens(legacy_wire, monkeypatch, tmp_path):
    from compartment import packs
    import numpy as np

    identity = packs.new_identity("author")
    blob = packs.build_pack(
        name="demo", version="1", description="d",
        records=[{"text": "packed memory", "namespace": "main", "tags": [],
                  "importance": 0.5}],
        vectors=np.zeros((1, 8), dtype=np.float32),
        model={"name": "m", "sha256": "x", "dim": 8},
        identity=identity, passphrase="packpass")
    monkeypatch.undo()

    header, records, _ = packs.read_pack(blob, passphrase="packpass")
    assert records[0]["text"] == "packed memory"


def test_a_vault_written_now_round_trips():
    """Belt and braces: the current constants agree with themselves."""
    master = crypto.new_key()
    slot = crypto.make_passphrase_slot(master, "correct horse")
    assert crypto.open_slot(slot, "correct horse") == master
    rk, wrapped = crypto.new_record_key(master, "rec-1")
    assert crypto.unwrap_record_key(master, "rec-1", wrapped) == rk


def _vec():
    import numpy as np
    return np.zeros(8, dtype=np.float32)
