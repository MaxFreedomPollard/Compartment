"""The .vault on-disk format (see FORMAT.md for the byte-level spec).

Layout:
    magic "NUCV" | version u16 | header_len u32 | header JSON (plaintext, canonical)
    payload ciphertext (AEAD-sealed, length in header)
    journal: zero or more entries, each: u32 len | AEAD-sealed entry

Invariant I2: plaintext never touches disk. The payload and every journal
entry are sealed before any byte is written. lock/save rewrites the file
atomically (temp file → fsync → rename). Journal appends are fsync'd, so an
acknowledged write survives kill -9; a truncated *final* entry is an
unacknowledged write and is discarded on open with a notice - any other
malformed byte is a tamper error.
"""
from __future__ import annotations

import io
import json
import os
import struct
import time
from dataclasses import dataclass, field

from nacl.signing import SigningKey, VerifyKey

from . import crypto, wire
from .crypto import CryptoError, TamperError

MAGIC = b"NUCV"
FORMAT_VERSION = 1

# The vault holds every memory an agent has, so it is owner-only. The mode is
# applied to the temp file BEFORE the atomic rename, because the rename gives
# the target the temp file's mode: creating the temp at the umask default and
# renaming it over a 0600 vault would silently widen the vault to 0644.
VAULT_MODE = 0o600

# A journal entry is one sealed record operation. Anything claiming to be
# larger than this is a corrupt length prefix, not a real entry, and must not
# be mistaken for a write that was torn off by a crash.
MAX_JOURNAL_ENTRY = 64 * 1024 * 1024


class VaultFormatError(CryptoError):
    """The file is not a valid Compartment vault (or a newer, unknown version)."""


@dataclass
class VaultHeader:
    vault_id: str
    created: str
    keyslots: list[dict]
    payload_len: int
    model: dict
    manifest: dict | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "vault_id": self.vault_id,
            "created": self.created,
            "keyslots": self.keyslots,
            "payload_len": self.payload_len,
            "model": self.model,
            "manifest": self.manifest,
            "extra": self.extra,
        }

    @staticmethod
    def from_json(d: dict) -> "VaultHeader":
        return VaultHeader(
            vault_id=d["vault_id"],
            created=d["created"],
            keyslots=d["keyslots"],
            payload_len=d["payload_len"],
            model=d["model"],
            manifest=d.get("manifest"),
            extra=d.get("extra", {}),
        )


def _atomic_replace(tmp: str, path: str) -> None:
    """os.replace, tolerant of the transient sharing violations Windows raises
    when another process momentarily holds a handle to `path`.

    Readers open the vault with a plain open() (Vault.unlock, `compartment status`,
    the dashboard's stale-reopen, the MCP/Hermes providers). CPython opens
    files on Windows without FILE_SHARE_DELETE, so a rename-over an open target
    raises PermissionError (WinError 5/32) - and antivirus/search-indexer
    handles do the same. POSIX allows rename-over-open, so this is a straight
    replace there; on Windows we retry briefly so a concurrent reader can't
    make a save/lock/shred fail outright."""
    if os.name != "nt":
        os.replace(tmp, path)
        return
    last: OSError | None = None
    for _ in range(20):            # ~1s worst case
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05)
    raise last if last is not None else OSError(f"could not replace {path}")


def _payload_aad(vault_id: str) -> bytes:
    return wire.payload(vault_id)[0]


def _journal_aad(vault_id: str, seq: int) -> bytes:
    return wire.journal(vault_id, seq)[0]


# ---------------------------------------------------------------------------
# TLV payload container: named binary sections inside the sealed payload.
# ---------------------------------------------------------------------------

def pack_sections(sections: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    out.write(struct.pack(">I", len(sections)))
    for name, data in sections.items():
        nb = name.encode("utf-8")
        out.write(struct.pack(">H", len(nb)))
        out.write(nb)
        out.write(struct.pack(">Q", len(data)))
        out.write(data)
    return out.getvalue()


def unpack_sections(blob: bytes) -> dict[str, bytes]:
    buf = io.BytesIO(blob)
    (count,) = struct.unpack(">I", buf.read(4))
    sections: dict[str, bytes] = {}
    for _ in range(count):
        (nlen,) = struct.unpack(">H", buf.read(2))
        name = buf.read(nlen).decode("utf-8")
        (dlen,) = struct.unpack(">Q", buf.read(8))
        data = buf.read(dlen)
        if len(data) != dlen:
            raise VaultFormatError("Payload section truncated")
        sections[name] = data
    return sections


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@dataclass
class LoadedVaultFile:
    header: VaultHeader
    payload_ct: bytes
    journal_cts: list[bytes]
    truncated_tail: bool  # a partial (crashed, unacknowledged) final journal entry was discarded
    tail_bytes: int = 0   # how many bytes that partial tail occupied


def read_vault_file(path: str) -> LoadedVaultFile:
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 10 or raw[:4] != MAGIC:
        raise VaultFormatError(f"{path} is not a Compartment vault (bad magic)")
    (version,) = struct.unpack(">H", raw[4:6])
    if version != FORMAT_VERSION:
        raise VaultFormatError(
            f"Vault format version {version} is not supported by this build "
            f"(supported: {FORMAT_VERSION})"
        )
    (hlen,) = struct.unpack(">I", raw[6:10])
    if len(raw) < 10 + hlen:
        raise VaultFormatError("Vault header truncated")
    try:
        header = VaultHeader.from_json(json.loads(raw[10 : 10 + hlen]))
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError,
            ValueError) as exc:
        raise VaultFormatError(
            f"Vault header is corrupt (modified or damaged): {exc}") from exc

    pos = 10 + hlen
    payload_ct = raw[pos : pos + header.payload_len]
    if len(payload_ct) != header.payload_len:
        raise VaultFormatError("Vault payload truncated")
    pos += header.payload_len

    journal_cts: list[bytes] = []
    truncated_tail = False
    tail_bytes = 0
    while pos < len(raw):
        if pos + 4 > len(raw):
            # fewer than four bytes left: a length prefix itself torn in half
            truncated_tail = True
            tail_bytes = len(raw) - pos
            break
        (elen,) = struct.unpack(">I", raw[pos : pos + 4])
        if elen > MAX_JOURNAL_ENTRY:
            # A crash while appending leaves a SHORT entry whose declared
            # length is honest. A length this large is the prefix itself being
            # corrupt, and treating it as a torn tail would silently discard
            # every acknowledged entry after it.
            raise TamperError(
                f"Journal entry at byte {pos} declares {elen} bytes, beyond the "
                f"{MAX_JOURNAL_ENTRY} byte maximum. The length prefix is "
                "corrupt, so entries after it cannot be located. Refusing to "
                "open rather than discarding them.")
        entry = raw[pos + 4 : pos + 4 + elen]
        if len(entry) != elen:
            # Ran out of file mid-entry. Consistent with a crash during append,
            # so the caller may discard it - but it is also what a corrupt
            # length prefix looks like, so the caller preserves the original
            # file before compacting rather than trusting this reading.
            truncated_tail = True
            tail_bytes = len(raw) - pos
            break
        journal_cts.append(entry)
        pos += 4 + elen
    return LoadedVaultFile(header, payload_ct, journal_cts, truncated_tail,
                           tail_bytes)


def decrypt_payload(header: VaultHeader, payload_ct: bytes, master_key: bytes) -> dict[str, bytes]:
    plain = crypto.unseal_any(master_key, payload_ct, *wire.payload(header.vault_id))
    return unpack_sections(plain)


def decrypt_journal(header: VaultHeader, journal_cts: list[bytes], master_key: bytes) -> list[dict]:
    """Entries are decrypted one at a time and each falls back on its own, so
    a vault upgraded mid-journal (older entries, newer appends) still replays."""
    entries = []
    for seq, ct in enumerate(journal_cts):
        plain = crypto.unseal_any(master_key, ct, *wire.journal(header.vault_id, seq))
        entries.append(json.loads(plain))
    return entries


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_vault_file(
    path: str,
    header: VaultHeader,
    sections: dict[str, bytes],
    master_key: bytes,
    signing_key: SigningKey | None = None,
) -> None:
    """Seal sections and atomically (re)write the vault with an empty journal."""
    payload_ct = crypto.seal(
        master_key, pack_sections(sections), aad=_payload_aad(header.vault_id)
    )
    header.payload_len = len(payload_ct)
    if signing_key is not None:
        header.manifest = _make_manifest(header, payload_ct, signing_key)
    else:
        # The payload is resealed with a fresh nonce on every save, so its
        # sha256 changes even when the plaintext does not. A manifest signed
        # against an earlier payload can therefore never match again, and
        # keeping it would make `verify` report tampering on a vault nobody
        # touched. Modifying a sealed vault ends the seal: drop the manifest
        # so the file honestly describes itself as unsigned.
        header.manifest = None
    hjson = crypto.canonical_json(header.to_json())
    tmp = path + ".tmp"
    # os.open with an explicit mode, not open(): the temp file's mode becomes
    # the vault's mode after the rename below, so creating it at the umask
    # default (commonly 0644) reopens the vault to every local account on
    # every save, including one the user had chmod'd to 0600.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, VAULT_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">H", FORMAT_VERSION))
        f.write(struct.pack(">I", len(hjson)))
        f.write(hjson)
        f.write(payload_ct)
        f.flush()
        os.fsync(f.fileno())
    # O_CREAT only sets the mode when it actually creates the file, so a temp
    # left behind by an earlier run keeps its old mode. Set it either way.
    os.chmod(tmp, VAULT_MODE)
    _atomic_replace(tmp, path)


def append_journal_entry(path: str, header: VaultHeader, seq: int, entry: dict, master_key: bytes) -> None:
    ct = crypto.seal(
        master_key,
        crypto.canonical_json(entry),
        aad=_journal_aad(header.vault_id, seq),
    )
    with open(path, "ab") as f:
        f.write(struct.pack(">I", len(ct)))
        f.write(ct)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Signed manifest (vault sealing) - verifiable without any key.
# ---------------------------------------------------------------------------

def _make_manifest(header: VaultHeader, payload_ct: bytes, signing_key: SigningKey) -> dict:
    body = {
        "creator": header.extra.get("creator", "unknown"),
        "created": header.created,
        "vault_id": header.vault_id,
        "content_sha256": crypto.sha256(payload_ct),
        "signer_pub": signing_key.verify_key.encode().hex(),
    }
    sig = signing_key.sign(crypto.canonical_json(body)).signature
    return {**body, "sig": sig.hex()}


def verify_manifest(loaded: LoadedVaultFile) -> dict:
    """Verify the signed manifest of a sealed vault. No key material needed."""
    m = loaded.header.manifest
    if not m:
        raise VaultFormatError("Vault is not signed (no manifest)")
    body = {k: v for k, v in m.items() if k != "sig"}
    try:
        VerifyKey(bytes.fromhex(m["signer_pub"])).verify(
            crypto.canonical_json(body), bytes.fromhex(m["sig"])
        )
    except Exception as exc:
        raise TamperError("Vault manifest signature is INVALID") from exc
    actual = crypto.sha256(loaded.payload_ct)
    if actual != m["content_sha256"]:
        raise TamperError(
            "Vault payload does not match its signed manifest (content was modified)"
        )
    if loaded.journal_cts:
        raise TamperError(
            "Sealed vault has journal entries appended after signing"
        )
    return m
