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


def test_a_keyslot_kdf_out_of_range_is_refused_before_deriving(monkeypatch):
    """A keyslot's KDF settings sit in the plaintext header, and the check
    that exposes an edit can only run once the KDF has finished: a header
    asking for 4 TiB of memory, or 10**9 passes, made an unlock fill memory
    or run for days. A stand-in records each derivation instead of running
    it, so nothing here allocates more than the test itself."""
    asked = []

    def argon2(**kwargs):
        asked.append((kwargs["time_cost"], kwargs["memory_cost"],
                      kwargs["parallelism"]))
        return bytes(crypto.KEY_LEN)
    monkeypatch.setattr(crypto, "hash_secret_raw", argon2)

    def slot(**kdf):
        return {"type": "passphrase", "kdf": {**crypto.DEFAULT_KDF, **kdf},
                "salt": "00" * 16, "wrapped": "00" * 64}
    for bad in ({"memory_kib": 2**32 - 1}, {"time_cost": 10**9},
                {"time_cost": 0}, {"parallelism": 0}, {"parallelism": 17},
                {"memory_kib": 1024 * 1024 + 1}, {"memory_kib": 31},
                {"time_cost": "3"}, {"parallelism": True}):
        with pytest.raises(TamperError, match="KDF parameters out of range"):
            crypto.open_slot(slot(**bad), "pw")
    assert asked == []
    # The widest settings allowed, and the defaults, are derived as before;
    # the stand-in's key then fails to open the slot, as a wrong one would.
    for ok in ({"time_cost": 16, "parallelism": 16, "memory_kib": 1024 * 1024},
               {}):
        with pytest.raises(TamperError, match="Integrity check failed"):
            crypto.open_slot(slot(**ok), "pw")
    assert asked == [(16, 1024 * 1024, 16), (3, 65536, 4)]


def _slot_with_salt(salt):
    return {"type": "passphrase", "salt": salt, "wrapped": "00" * 64,
            "kdf": {"alg": "argon2id", "time_cost": 1, "memory_kib": 8,
                    "parallelism": 1}}


def _recording_argon2(monkeypatch):
    """The real Argon2id, with every salt it is asked to derive with."""
    real, salts = crypto.hash_secret_raw, []

    def argon2(**kwargs):
        salts.append(kwargs["salt"])
        return real(**kwargs)
    monkeypatch.setattr(crypto, "hash_secret_raw", argon2)
    return salts


@pytest.mark.parametrize("salt", ["zz", 16, "00" * 7])
def test_a_keyslot_salt_not_hex_or_too_short_is_refused_before_deriving(
        salt, monkeypatch):
    """The salt sits in the plaintext header beside the KDF settings. One
    that was not hex escaped open_slot as bytes.fromhex's ValueError (or
    TypeError), and one shorter than the 8 bytes Argon2id accepts escaped
    unlock as Argon2's own HashingError, "Salt is too short": none of them
    an error a caller knows as a damaged vault."""
    derived = _recording_argon2(monkeypatch)
    with pytest.raises(TamperError, match="salt"):
        crypto.open_slot(_slot_with_salt(salt), "pw")
    assert derived == []


def test_an_eight_byte_keyslot_salt_is_still_derived(monkeypatch):
    """Eight bytes is Argon2id's own floor, so it is derived as before; the
    all-zero wrapped key then fails to open, as a wrong one would."""
    derived = _recording_argon2(monkeypatch)
    with pytest.raises(TamperError, match="Integrity check failed"):
        crypto.open_slot(_slot_with_salt("00" * 8), "pw")
    assert derived == [bytes(8)]
