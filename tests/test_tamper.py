"""Tamper suite: flip bytes anywhere → loud integrity errors, never bad data."""
import json
import struct

import numpy as np
import pytest

from compartment import packs
from compartment.crypto import CryptoError, TamperError
from compartment.vault import Vault
from compartment.vaultfile import VaultFormatError, read_vault_file

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
    packs.read_pack(blob)  # verifies clean

    body_bad = bytearray(blob)
    body_bad[-2] ^= 0xFF  # body tamper → content hash mismatch
    with pytest.raises(TamperError):
        packs.read_pack(bytes(body_bad))

    (hlen,) = struct.unpack(">I", blob[6:10])
    header = json.loads(blob[10:10 + hlen])
    header["records"] = 999  # header tamper → signature invalid
    import compartment.crypto as c
    hj = c.canonical_json(header)
    forged = blob[:6] + struct.pack(">I", len(hj)) + hj + blob[10 + hlen:]
    with pytest.raises(TamperError):
        packs.read_pack(forged)
