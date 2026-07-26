import pytest

from compartment import crypto
from compartment.crypto import TamperError


def test_seal_unseal_roundtrip():
    key = crypto.new_key()
    for msg in (b"", b"x", b"hello world" * 1000):
        assert crypto.unseal(key, crypto.seal(key, msg, b"aad"), b"aad") == msg


def test_aad_binding():
    key = crypto.new_key()
    blob = crypto.seal(key, b"secret", b"context-a")
    with pytest.raises(TamperError):
        crypto.unseal(key, blob, b"context-b")


def test_wrong_key_fails():
    blob = crypto.seal(crypto.new_key(), b"secret")
    with pytest.raises(TamperError):
        crypto.unseal(crypto.new_key(), blob)


def test_ciphertext_bitflip_detected():
    key = crypto.new_key()
    blob = bytearray(crypto.seal(key, b"payload bytes"))
    blob[-1] ^= 0x01
    with pytest.raises(TamperError):
        crypto.unseal(key, bytes(blob))


def test_keyslots_passphrase_and_recovery():
    master = crypto.new_key()
    slot = crypto.make_passphrase_slot(master, "pw1")
    rec_slot, words = crypto.make_recovery_slot(master)
    assert len(words) == 16 and all(w in crypto.WORDLIST for w in words)
    assert crypto.unwrap_master([slot, rec_slot], "pw1") == master
    assert crypto.unwrap_master([slot, rec_slot], " ".join(words)) == master
    with pytest.raises(TamperError):
        crypto.unwrap_master([slot, rec_slot], "wrong")


def test_record_key_crypto_shred_semantics():
    master = crypto.new_key()
    rk, wrapped = crypto.new_record_key(master, "rec1")
    ct = crypto.seal(rk, b"the memory text")
    # with the wrapped key, recoverable:
    rk2 = crypto.unwrap_record_key(master, "rec1", wrapped)
    assert crypto.unseal(rk2, ct) == b"the memory text"
    # wrapped key bound to record id (can't be replayed onto another record):
    with pytest.raises(TamperError):
        crypto.unwrap_record_key(master, "rec2", wrapped)


def test_wordlist_invariants():
    assert len(crypto.WORDLIST) == 256
    assert len(set(crypto.WORDLIST)) == 256
