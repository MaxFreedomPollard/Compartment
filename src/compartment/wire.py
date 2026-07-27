"""The byte strings Compartment writes into vaults, packs and session files.

None of this is naming. Every constant below is AEAD associated data, a KDF
context label or a credential identifier, which means it is baked into bytes
that are already on disk. Changing one does not rename anything: it makes data
written under the old spelling undecryptable.

So each constant comes as a pair. The plain name is what gets written. The
`_LEGACY` tuple beside it is every spelling that has ever been written and
must still open. Compartment was called engRAM until 1.15.0, so anything
created before then carries `engram` in its bytes.

Reading tries the current spelling first and falls back through the legacy
ones (`crypto.unseal_any`). Writing only ever uses the current spelling. A
vault made by an older build therefore opens normally, and `Vault.unlock`
rewrites it in the current format once, on the spot. There is nothing for the
user to run and nothing to convert.

If you are modifying this file: to add a label, give it a value and an empty
legacy tuple. To change one, move the old value into its legacy tuple and
never delete it. A legacy entry removed is a vault nobody can open again.
"""
from __future__ import annotations

import struct

# Bumped when any label below changes. `Vault.unlock` compares it against
# `header.extra["wire"]` to decide whether a vault needs rewriting, so this is
# the one number that says "these bytes are current".
#   1 = engRAM spelling, everything up to 2.1
#   2 = Compartment spelling
WIRE_FORMAT = 2

# --- keyslots --------------------------------------------------------------

# Associated data wrapping the vault master key. The most load-bearing string
# in the project: if a keyslot will not unseal, the vault is gone.
KEYSLOT = b"compartment-keyslot"
KEYSLOT_LEGACY = (b"engram-keyslot",)

# Domain separator between the knowledge factor (passphrase) and the
# possession factor (keyfile bytes). This one is a KDF *input*, not associated
# data, so an old separator produces a different wrap key and costs a second
# Argon2id pass to migrate. See crypto.open_slot.
KEYFILE_SEP = b"\x1f compartment-2fa \x1f"
KEYFILE_SEP_LEGACY = (b"\x1f engram-2fa \x1f",)

# --- vault file ------------------------------------------------------------

_PAYLOAD = b"compartment-payload:"
_PAYLOAD_LEGACY = (b"engram-payload:",)

_JOURNAL = b"compartment-journal:"
_JOURNAL_LEGACY = (b"engram-journal:",)

# --- records ---------------------------------------------------------------

_RECORD_KEY = b"compartment-record:"
_RECORD_KEY_LEGACY = (b"engram-record:",)

_RECORD_BODY = b"compartment-record-body:"
_RECORD_BODY_LEGACY = (b"engram-record-body:",)

# --- memory packs ----------------------------------------------------------

_PACK = b"compartment-pack:"
_PACK_LEGACY = (b"engram-pack:",)

# --- boot session credential ----------------------------------------------

_SESSION = b"compartment-session:"
_SESSION_LEGACY = (b"engram-session:",)

# Hashed into the boot-bound wrap key. Not associated data, but the same
# hazard: change it and every live session file stops opening, which locks
# every unlocked vault on the machine until the user types a passphrase again.
SESSION_TOKEN = "compartment-session-v3"
SESSION_TOKEN_LEGACY = ("engram-session-v2",)

# --- macOS Keychain --------------------------------------------------------

# Names an item macOS has already stored. A rename here does not move the
# item, it just stops finding it, so keychain_get migrates on the way past.
KEYCHAIN_SERVICE = "compartment-vault:"
KEYCHAIN_SERVICE_LEGACY = ("engram-vault:",)

KEYCHAIN_ACCOUNT = "compartment"
KEYCHAIN_ACCOUNT_LEGACY = ("engram",)


# ---------------------------------------------------------------------------
# Labels that carry an identifier. Each returns (current, legacy_forms) ready
# to splat into crypto.unseal_any.
# ---------------------------------------------------------------------------

def _with(current: bytes, legacy: tuple[bytes, ...], tail: bytes
          ) -> tuple[bytes, tuple[bytes, ...]]:
    return current + tail, tuple(old + tail for old in legacy)


def payload(vault_id: str) -> tuple[bytes, tuple[bytes, ...]]:
    """Associated data over the whole sealed payload."""
    return _with(_PAYLOAD, _PAYLOAD_LEGACY, vault_id.encode())


def journal(vault_id: str, seq: int) -> tuple[bytes, tuple[bytes, ...]]:
    """Associated data over one journal entry. The sequence number is bound in
    so entries cannot be reordered or replayed."""
    return _with(_JOURNAL, _JOURNAL_LEGACY,
                 vault_id.encode() + b":" + struct.pack(">Q", seq))


def record_key(record_id: str) -> tuple[bytes, tuple[bytes, ...]]:
    """Associated data wrapping one record's data key (the crypto-shred unit)."""
    return _with(_RECORD_KEY, _RECORD_KEY_LEGACY, record_id.encode())


def record_body(record_id: str) -> tuple[bytes, tuple[bytes, ...]]:
    """Associated data over one record's ciphertext."""
    return _with(_RECORD_BODY, _RECORD_BODY_LEGACY, record_id.encode())


def pack(name: str) -> tuple[bytes, tuple[bytes, ...]]:
    """Associated data over an encrypted memory pack body."""
    return _with(_PACK, _PACK_LEGACY, name.encode())


def session(vault_path: str) -> tuple[bytes, tuple[bytes, ...]]:
    """Associated data over a boot-session unlock credential."""
    return _with(_SESSION, _SESSION_LEGACY, vault_path.encode())
