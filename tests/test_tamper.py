"""Tamper suite: flip bytes anywhere → loud integrity errors, never bad data."""
import json
import struct

import numpy as np
import pytest

from compartment import crypto, packs
from compartment.crypto import CryptoError, TamperError
from compartment.vault import Vault
from compartment.vaultfile import VaultFormatError, VaultHeader, read_vault_file

from conftest import PASS


def _flip(path, offset_frac, out):
    raw = bytearray(open(path, "rb").read())
    raw[int(len(raw) * offset_frac)] ^= 0xFF
    open(out, "wb").write(bytes(raw))
    return out


def test_payload_tamper_detected(vault, vault_path, tmp_path):
    vault.store("sensitive memory", caller="test")
    vault.lock()
    bad = _flip(vault_path, 0.7, str(tmp_path / "bad.vault"))
    with pytest.raises((TamperError, VaultFormatError)):
        Vault.unlock(bad, passphrase=PASS)


def test_header_tamper_detected(vault, vault_path, tmp_path):
    vault.lock()
    bad = _flip(vault_path, 0.001, str(tmp_path / "bad2.vault"))
    with pytest.raises((TamperError, VaultFormatError, CryptoError)):
        Vault.unlock(bad, passphrase=PASS)


def test_journal_tamper_detected(vault, vault_path, tmp_path):
    vault.store("acknowledged write", caller="test")  # journaled, not compacted
    del vault
    raw = bytearray(open(vault_path, "rb").read())
    raw[-3] ^= 0xFF  # inside the journal entry ciphertext
    bad = str(tmp_path / "bad3.vault")
    open(bad, "wb").write(bytes(raw))
    with pytest.raises(TamperError):
        Vault.unlock(bad, passphrase=PASS)


def test_truncated_final_journal_entry_is_unacknowledged_crash(vault, vault_path, tmp_path):
    vault.store("first", caller="test")
    vault.store("second", caller="test")
    del vault
    raw = open(vault_path, "rb").read()
    crashed = str(tmp_path / "crashed.vault")
    open(crashed, "wb").write(raw[:-5])  # cut mid-final-entry, like kill -9
    v = Vault.unlock(crashed, passphrase=PASS)
    assert v.db.count() == 1  # first write survived; unacknowledged tail dropped


def test_wrong_passphrase(vault, vault_path):
    vault.lock()
    with pytest.raises(TamperError):
        Vault.unlock(vault_path, passphrase="NotThePassphrase")


def test_unknown_format_version(vault_path, vault, tmp_path):
    vault.lock()
    raw = bytearray(open(vault_path, "rb").read())
    raw[4:6] = struct.pack(">H", 99)
    bad = str(tmp_path / "future.vault")
    open(bad, "wb").write(bytes(raw))
    with pytest.raises(VaultFormatError):
        read_vault_file(bad)


def test_pack_signature_and_content_tamper():
    ident = packs.new_identity("tester")
    vecs = np.random.rand(2, 4).astype(np.float32)
    blob = packs.build_pack(
        name="t", version="1.0.0", description="", identity=ident,
        records=[{"text": "a"}, {"text": "b"}], vectors=vecs,
        model={"name": "m", "sha256": "x", "dim": 4})
    trust = [ident["pub_hex"]]                     # author trusted explicitly
    packs.read_pack(blob, trusted_keys=trust)  # verifies clean

    body_bad = bytearray(blob)
    body_bad[-2] ^= 0xFF  # body tamper → content hash mismatch
    with pytest.raises(TamperError):
        packs.read_pack(bytes(body_bad), trusted_keys=trust)

    (hlen,) = struct.unpack(">I", blob[6:10])
    header = json.loads(blob[10:10 + hlen])
    header["records"] = 999  # header tamper → signature invalid
    import compartment.crypto as c
    hj = c.canonical_json(header)
    forged = blob[:6] + struct.pack(">I", len(hj)) + hj + blob[10 + hlen:]
    with pytest.raises(TamperError):
        packs.read_pack(forged, trusted_keys=trust)


def _with_header(vault_path, out, edit):
    """A copy of the vault whose plaintext header `edit` changed in place,
    or replaced with what it returned."""
    raw = open(vault_path, "rb").read()
    (hlen,) = struct.unpack(">I", raw[6:10])
    header = json.loads(raw[10:10 + hlen])
    replaced = edit(header)
    hj = crypto.canonical_json(header if replaced is None else replaced)
    open(out, "wb").write(raw[:6] + struct.pack(">I", len(hj)) + hj
                          + raw[10 + hlen:])
    return out


def _without(d, key):
    del d[key]


MALFORMED_HEADERS = {
    "the header is a list": lambda h: [h],
    "vault_id is a number": lambda h: h.update(vault_id=7),
    "payload_len is a string": lambda h: h.update(payload_len="abc"),
    "payload_len is negative": lambda h: h.update(payload_len=-1),
    "keyslots is a string": lambda h: h.update(keyslots="x"),
    "a keyslot is a string": lambda h: h.update(keyslots=["x"]),
    "a keyslot has no type": lambda h: _without(h["keyslots"][0], "type"),
    "a keyslot has no salt": lambda h: _without(h["keyslots"][0], "salt"),
    "a keyslot's salt is not hex": lambda h: h["keyslots"][0].update(salt="zz"),
    "a keyslot's salt is too short":
        lambda h: h["keyslots"][0].update(salt="00" * 7),
    "a keyslot's kdf is a string":
        lambda h: h["keyslots"][0].update(kdf="argon2id"),
    "model is a list": lambda h: h.update(model=[]),
    "model has no dim": lambda h: _without(h["model"], "dim"),
    "model.dim is a string": lambda h: h["model"].update(dim="384"),
    "extra is a list": lambda h: h.update(extra=[]),
    "extra.wire is a string": lambda h: h["extra"].update(wire="2"),
}


def test_a_malformed_header_is_reported_as_corrupt(vault, vault_path,
                                                   tmp_path, monkeypatch):
    """The header is plaintext. A field of the wrong type, or a missing one,
    escaped unlock as a TypeError, KeyError or AttributeError from deep
    inside it, which no caller recognises as a vault error: a traceback
    instead of "the vault header is corrupt"."""
    monkeypatch.setattr(crypto, "ATTEMPT_INTERVAL_SECONDS", 0.0)
    vault.lock()
    escaped = []
    for i, (what, edit) in enumerate(MALFORMED_HEADERS.items()):
        bad = _with_header(vault_path, str(tmp_path / f"bad{i}.vault"), edit)
        try:
            Vault.unlock(bad, passphrase=PASS)
            escaped.append(f"{what}: opened")
        except CryptoError:
            pass
        except Exception as exc:                        # noqa: BLE001
            escaped.append(f"{what}: {type(exc).__name__}: {exc}")
    assert escaped == []


@pytest.mark.parametrize("key", ["vault_id", "created", "keyslots",
                                 "payload_len", "model"])
def test_a_header_missing_a_field_is_reported_as_corrupt(key):
    """from_json read `created` without checking that it was there, so a
    header without it raised a bare KeyError from the parser, where every
    other field the header needs is reported as the header being corrupt."""
    d = {"vault_id": "v", "created": "2026-01-01T00:00:00+00:00",
         "keyslots": [], "payload_len": 0, "model": {"dim": 384}}
    del d[key]
    with pytest.raises(VaultFormatError, match=f"corrupt.*{key}"):
        VaultHeader.from_json(d)


def test_unknown_header_fields_still_open(vault, vault_path, tmp_path):
    """Additive header fields are allowed within a format version."""
    vault.lock()

    def add(h):
        h["future_field"] = {"anything": 1}
        h["extra"]["future_setting"] = "kept"
        h["keyslots"][0]["future_slot_field"] = True
    v = Vault.unlock(_with_header(vault_path, str(tmp_path / "f.vault"), add),
                     passphrase=PASS)
    assert v.header.extra["future_setting"] == "kept"
