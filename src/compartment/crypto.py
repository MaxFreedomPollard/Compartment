"""Compartment cryptography: XChaCha20-Poly1305 AEAD, Argon2id KDF, LUKS-style keyslots.

Everything at rest is sealed with AEAD (tamper-evident by construction).
A random 256-bit vault master key is wrapped by one or more keyslots;
per-record data keys are wrapped by the master key to enable crypto-shred.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from typing import Any

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)

from . import wire

KEY_LEN = 32
NONCE_LEN = 24

# Interactive-strength Argon2id defaults, sized for an 8GB-RAM baseline machine.
DEFAULT_KDF = {"alg": "argon2id", "time_cost": 3, "memory_kib": 65536, "parallelism": 4}


class CryptoError(Exception):
    """Loud, specific crypto failure. Never swallowed."""


class TamperError(CryptoError):
    """AEAD authentication failed: the data was modified or the key is wrong."""


def new_key() -> bytes:
    return secrets.token_bytes(KEY_LEN)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_key(secret: bytes, salt: bytes, kdf: dict | None = None) -> bytes:
    kdf = kdf or DEFAULT_KDF
    if kdf.get("alg") != "argon2id":
        raise CryptoError(f"Unsupported KDF algorithm: {kdf.get('alg')!r}")
    return hash_secret_raw(
        secret=secret,
        salt=salt,
        time_cost=int(kdf["time_cost"]),
        memory_cost=int(kdf["memory_kib"]),
        parallelism=int(kdf["parallelism"]),
        hash_len=KEY_LEN,
        type=Argon2Type.ID,
    )


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """AEAD-encrypt: returns nonce || ciphertext+tag."""
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)
    return nonce + ct


def unseal(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    """AEAD-decrypt with authentication. Raises TamperError on any modification."""
    if len(blob) < NONCE_LEN + 16:
        raise TamperError("Sealed blob is truncated")
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return crypto_aead_xchacha20poly1305_ietf_decrypt(ct, aad, nonce, key)
    except Exception as exc:
        raise TamperError(
            "Integrity check failed: data was tampered with or the key is wrong"
        ) from exc


def unseal_which(key: bytes, blob: bytes, aad: bytes,
                 legacy: tuple[bytes, ...] = ()) -> tuple[bytes, bytes]:
    """unseal(), accepting associated data written by an older build.

    Returns (plaintext, the aad that actually authenticated), so a caller can
    tell a migrated blob from one still carrying a legacy label and rewrite it.

    The current spelling is tried first, so anything already migrated costs
    exactly one AEAD open. Legacy spellings are tried in order only after that
    fails, which is what lets a pre-rename vault open at all. If none of them
    authenticate the data really is tampered with, or the key really is wrong,
    and the original error is raised."""
    try:
        return unseal(key, blob, aad=aad), aad
    except TamperError:
        for old in legacy:
            try:
                return unseal(key, blob, aad=old), old
            except TamperError:
                continue
        raise


def unseal_any(key: bytes, blob: bytes, aad: bytes,
               legacy: tuple[bytes, ...] = ()) -> bytes:
    """unseal_which() when the caller does not care which label was used."""
    return unseal_which(key, blob, aad, legacy)[0]


def wipe(buf: bytearray) -> None:
    """Best-effort zeroization (Python cannot guarantee no copies exist)."""
    for i in range(len(buf)):
        buf[i] = 0


# ---------------------------------------------------------------------------
# Keyslots (LUKS-style): each slot wraps the same master key.
# ---------------------------------------------------------------------------

def make_passphrase_slot(master_key: bytes, passphrase: str, kdf: dict | None = None) -> dict:
    kdf = dict(kdf or DEFAULT_KDF)
    salt = secrets.token_bytes(16)
    wrap_key = derive_key(passphrase.encode("utf-8"), salt, kdf)
    return {
        "type": "passphrase",
        "kdf": kdf,
        "salt": salt.hex(),
        "wrapped": seal(wrap_key, master_key, aad=wire.KEYSLOT).hex(),
    }


def make_recovery_slot(master_key: bytes, kdf: dict | None = None) -> tuple[dict, list[str]]:
    """LEGACY (read-path only): Compartment no longer generates credentials of any
    kind - the user's own passphrase is the only knowledge factor. This
    function remains so vaults created by older versions, which auto-
    generated a 16-word recovery phrase, still open with that phrase."""
    raw = secrets.token_bytes(16)  # 128 bits
    words = [WORDLIST[b] for b in raw]
    slot = make_passphrase_slot(master_key, " ".join(words), kdf)
    slot["type"] = "recovery"
    return slot, words


# Domain separator between the knowledge factor (passphrase) and the
# possession factor (keyfile bytes) before the KDF. Both factors feed
# Argon2id, so two-factor unlock is enforced by arithmetic, not policy.
KEYFILE_SEP = wire.KEYFILE_SEP
KEYFILE_LEN = 32


def make_keyfile_slot(master_key: bytes, passphrase: str, keyfile: bytes,
                      kdf: dict | None = None) -> dict:
    """Two-factor keyslot: master key wrapped under
    Argon2id(passphrase ‖ SEP ‖ keyfile). Opening REQUIRES both factors."""
    if len(keyfile) < 16:
        raise CryptoError("Keyfile too short to be a real second factor")
    kdf = dict(kdf or DEFAULT_KDF)
    salt = secrets.token_bytes(16)
    secret = passphrase.encode("utf-8") + KEYFILE_SEP + keyfile
    wrap_key = derive_key(secret, salt, kdf)
    return {
        "type": "passphrase+keyfile",
        "kdf": kdf,
        "salt": salt.hex(),
        "keyfile_id": sha256(keyfile)[:16],   # UX only: detect the WRONG file
        "wrapped": seal(wrap_key, master_key, aad=wire.KEYSLOT).hex(),
    }


def _slot_secrets(slot: dict, secret: str, keyfile: bytes | None) -> list[bytes]:
    """The KDF inputs worth trying for this slot, current spelling first.

    Only a two-factor slot has more than one. Its separator is a KDF input
    rather than associated data, so an older separator derives a different
    wrap key and can only be ruled out by deriving again."""
    raw = secret.encode("utf-8")
    if slot["type"] != "passphrase+keyfile":
        return [raw]
    if keyfile is None:
        raise CryptoError("This keyslot requires a keyfile")
    return [raw + sep + keyfile
            for sep in (wire.KEYFILE_SEP, *wire.KEYFILE_SEP_LEGACY)]


def open_slot(slot: dict, secret: str, keyfile: bytes | None = None) -> bytes:
    """Unwrap the master key from one slot.

    A slot written before 2.2 carries the legacy associated data, and a
    two-factor one the legacy keyfile separator. Both still open. The slot is
    then rewritten in place under the current spelling, so it migrates the
    first time its credential is used; the caller persists that by saving the
    header. Re-sealing only happens when something was actually legacy, or
    every unlock would dirty the header and force a needless vault rewrite."""
    salt, kdf = bytes.fromhex(slot["salt"]), slot["kdf"]
    wrapped = bytes.fromhex(slot["wrapped"])
    failure: TamperError | None = None
    for i, raw in enumerate(_slot_secrets(slot, secret, keyfile)):
        wrap_key = derive_key(raw, salt, kdf)
        try:
            master, used = unseal_which(wrap_key, wrapped, wire.KEYSLOT,
                                        wire.KEYSLOT_LEGACY)
        except TamperError as exc:
            failure = exc
            continue
        if i:
            # legacy separator: the wrap key itself is stale, so the slot has
            # to be built again from scratch (a second Argon2id pass, once).
            fresh = make_keyfile_slot(master, secret, keyfile, kdf)
            fresh["type"] = slot["type"]
            slot.clear()
            slot.update(fresh)
        elif used != wire.KEYSLOT:
            # same wrap key, only the associated data moves. Free.
            slot["wrapped"] = seal(wrap_key, master, aad=wire.KEYSLOT).hex()
        return master
    raise failure if failure is not None else TamperError("Keyslot did not open")


def unwrap_master(keyslots: list[dict], secret: str,
                  keyfile: bytes | None = None) -> bytes:
    """Try the credential(s) against every keyslot; fail loudly if none opens."""
    needs_keyfile = False
    for slot in keyslots:
        if slot["type"] in ("passphrase", "recovery"):
            try:
                return open_slot(slot, secret)
            except TamperError:
                continue
        elif slot["type"] == "passphrase+keyfile":
            if keyfile is None:
                needs_keyfile = True
                continue
            if sha256(keyfile)[:16] != slot.get("keyfile_id"):
                raise CryptoError(
                    "That is not this vault's keyfile (contents do not match "
                    "the enrolled second factor)")
            try:
                return open_slot(slot, secret, keyfile)
            except TamperError:
                continue
    if needs_keyfile:
        raise CryptoError(
            "Two-factor unlock is enabled on this vault: a keyfile is "
            "required alongside the passphrase (compartment unlock --keyfile "
            "/path/to/compartment-2fa.key)")
    raise TamperError("Wrong passphrase (no keyslot opened)")


def normalize_recovery(text: str) -> str:
    return " ".join(text.strip().lower().split())


# ---------------------------------------------------------------------------
# Per-record keys → crypto-shred
# ---------------------------------------------------------------------------

def wrap_record_key(master_key: bytes, record_id: str, record_key: bytes) -> bytes:
    aad, _ = wire.record_key(record_id)
    return seal(master_key, record_key, aad=aad)


def new_record_key(master_key: bytes, record_id: str) -> tuple[bytes, bytes]:
    """Returns (record_key, wrapped_record_key). Destroying the wrapped key
    makes the record's ciphertext permanently undecryptable (crypto-shred)."""
    rk = new_key()
    return rk, wrap_record_key(master_key, record_id, rk)


def unwrap_record_key(master_key: bytes, record_id: str, wrapped: bytes) -> bytes:
    return unseal_any(master_key, wrapped, *wire.record_key(record_id))


# 256 short, common, distinct words for recovery phrases (16 words = 128 bits).
WORDLIST = (
    "acid actor alarm album alien amber angel ankle apple apron arrow atlas "
    "attic autumn bacon badge baker bamboo banjo barn basil beach beak bean "
    "bear beard beaver bell belt bench berry bird bison blade blanket blossom "
    "board boat bone book boot bottle bow bowl box brain branch brass bread "
    "brick bridge broom brush bucket bulb bull button cabin cactus cake camel "
    "camera canal candle canoe canyon card cargo carpet carrot castle cave "
    "cedar chain chair chalk cheese cherry chess chest chief chime cider "
    "circle city clam clay cliff clock cloud clover coal coast coin comet "
    "coral cork corn cotton cradle crane crater crayon cream crow crown cube "
    "cup curtain cycle daisy dawn deer delta desk dew dice dime dish dock "
    "dolphin dome donkey door dove dragon drum duck dune eagle earth easel "
    "echo eel egg elbow elm ember engine fabric falcon fan farm feather fern "
    "ferry field fig finch fire flag flame flask fleet flint flute foam fog "
    "forest fork fossil fox frame frost fruit galaxy garden gate gem geyser "
    "giant gift ginger glacier glass globe glove goat gold goose grain grape "
    "grass grove guitar hammer harbor harp hawk hazel heart hedge hill hive "
    "honey hook horn horse house husk ice igloo ink iris iron island ivory "
    "ivy jade jar jet jewel judge juice jungle kayak kettle key kite kiwi "
    "knee knife knot lace lake lamp lantern latch lava leaf ledge lemon lens "
    "level lily lime linen lion lizard lobster log loom lotus lumber lunar "
    "lynx machine magnet maple marble mask mast meadow melon mesa mint"
).split()
assert len(WORDLIST) == 256 and len(set(WORDLIST)) == 256
