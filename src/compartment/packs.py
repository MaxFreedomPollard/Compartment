"""Memory packs (.mpack): signed, versioned, offline-installable knowledge.

Format (see FORMAT.md):
    magic "NUCP" | version u16 | header_len u32 | header JSON | body
Header carries name/semver/creator, the embedding model (name+sha256+dim)
the vectors were computed with, the body's SHA-256, and a MANDATORY Ed25519
signature. Body = TLV sections: "records" (JSONL) + "vectors" (raw float32),
optionally AEAD-sealed with a pack passphrase.

TRUST: a pack cannot vouch for itself. The signature is verified against a
key from the TRUSTED SET, never against the `signer_pub` the pack carries -
that field is only an identifying hint. The trusted set is the project's
own verify key (PROJECT_PACK_KEY, below) plus whatever keys the operator
passes explicitly as `trusted_keys`. A pack signed by anything else is
refused, however internally consistent it is.

Install verifies signature + content hash FIRST and rejects loudly on any
mismatch. Matching model → precomputed vectors load directly (no compute).
Records land read-only in namespace packs/<name>.
"""
from __future__ import annotations

import json
import secrets
import struct

import numpy as np
from nacl.signing import SigningKey, VerifyKey

from . import crypto, wire
from .crypto import CryptoError, TamperError
from .vaultfile import pack_sections, unpack_sections

PACK_MAGIC = b"NUCP"
PACK_VERSION = 1


class PackError(CryptoError):
    pass


# ---------------------------------------------------------------------------
# Trust store (who is allowed to sign a pack)
# ---------------------------------------------------------------------------

#: Ed25519 verify key of the Compartment project's pack-signing identity.
#: This public half ships in the source tree on purpose - it is what makes the
#: bundled starter pack verifiable without trusting the file it arrived in.
#: The private half lives in tools/pack_identity.json, which is gitignored and
#: never leaves the maintainer's machine.
PROJECT_PACK_KEY = "2577018190ac8d39185854306ffbce84fd86feb6e2b20ab256c3bdcbff60d91e"

#: Trusted by default, with no way for a pack to add itself.
TRUSTED_PACK_KEYS: frozenset[str] = frozenset({PROJECT_PACK_KEY})


def _normalize_key(key: str) -> str:
    """A 64-char lowercase hex Ed25519 public key, or a loud error."""
    if not isinstance(key, str):
        raise PackError(f"Trusted key must be 64-char hex, got {type(key).__name__}")
    k = key.strip().lower()
    try:
        raw = bytes.fromhex(k)
    except ValueError:
        raise PackError(f"Trusted key {key!r} is not hex") from None
    if len(raw) != 32:
        raise PackError(f"Trusted key {key!r} is {len(raw)} bytes, "
                        "expected a 32-byte (64-hex-char) Ed25519 public key")
    return k


def resolve_trusted_keys(trusted_keys: str | list[str] | None = None
                         ) -> frozenset[str]:
    """The set of keys allowed to sign a pack for this call.

    Always contains PROJECT_PACK_KEY. `trusted_keys` (a hex key or a list of
    them) ADDS operator-chosen keys for this call only - that is the one and
    only way a third-party pack becomes installable.
    """
    if trusted_keys is None:
        return TRUSTED_PACK_KEYS
    if isinstance(trusted_keys, str):
        trusted_keys = [trusted_keys]
    return frozenset(TRUSTED_PACK_KEYS | {_normalize_key(k) for k in trusted_keys})


# ---------------------------------------------------------------------------
# Identity (pack authors)
# ---------------------------------------------------------------------------

def new_identity(name: str) -> dict:
    sk = SigningKey.generate()
    return {"signer": name, "seed_hex": sk.encode().hex(),
            "pub_hex": sk.verify_key.encode().hex()}


def load_signing_key(identity: dict) -> SigningKey:
    return SigningKey(bytes.fromhex(identity["seed_hex"]))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_pack(*, name: str, version: str, description: str, records: list[dict],
               vectors: np.ndarray, model: dict, identity: dict,
               passphrase: str | None = None) -> bytes:
    if not records:
        raise PackError("Refusing to build an empty pack")
    if vectors.shape[0] != len(records):
        raise PackError("records/vectors length mismatch")
    jsonl = "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n"
    body = pack_sections({
        "records": jsonl.encode("utf-8"),
        "vectors": np.ascontiguousarray(vectors, dtype=np.float32).tobytes(),
    })
    header: dict = {
        "name": name, "version": version, "description": description,
        "creator": identity["signer"],
        "model": model, "records": len(records),
        "encrypted": passphrase is not None,
    }
    if passphrase is not None:
        salt = secrets.token_bytes(16)
        key = crypto.derive_key(passphrase.encode(), salt)
        body = crypto.seal(key, body, aad=wire.pack(name)[0])
        header["kdf"] = crypto.DEFAULT_KDF
        header["salt"] = salt.hex()
    header["content_sha256"] = crypto.sha256(body)
    sk = load_signing_key(identity)
    # An identifying hint for the reader ("who claims to have signed this").
    # Readers verify against their trusted set, never against this field.
    header["signer_pub"] = sk.verify_key.encode().hex()
    header["sig"] = sk.sign(crypto.canonical_json(
        {k: v for k, v in header.items() if k != "sig"})).signature.hex()

    hjson = crypto.canonical_json(header)
    return (PACK_MAGIC + struct.pack(">H", PACK_VERSION)
            + struct.pack(">I", len(hjson)) + hjson + body)


# ---------------------------------------------------------------------------
# Read + verify
# ---------------------------------------------------------------------------

def read_pack(blob: bytes, passphrase: str | None = None, *,
              trusted_keys: str | list[str] | None = None
              ) -> tuple[dict, list[dict], np.ndarray]:
    """Verify signature + content hash, then return (header, records, vectors).

    The signature is checked against `trusted_keys` + the project key, never
    against the key inside the pack. `header["verified_by"]` is set to the
    trusted key that actually verified it.
    """
    if len(blob) < 10 or blob[:4] != PACK_MAGIC:
        raise PackError("Not a Compartment memory pack (bad magic)")
    (ver,) = struct.unpack(">H", blob[4:6])
    if ver != PACK_VERSION:
        raise PackError(f"Pack format version {ver} not supported")
    (hlen,) = struct.unpack(">I", blob[6:10])
    header = json.loads(blob[10:10 + hlen])
    body = blob[10 + hlen:]
    name = header.get("name", "?")
    trusted = resolve_trusted_keys(trusted_keys)

    # 1) signature over the header (which pins the content hash), verified with
    #    a TRUSTED key - header["signer_pub"] is a hint and is never trusted.
    signed = crypto.canonical_json({k: v for k, v in header.items() if k != "sig"})
    try:
        sig = bytes.fromhex(header["sig"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TamperError(f"Pack {name!r}: signature missing or malformed - "
                          "refusing to install") from exc
    verified_by = None
    for key_hex in sorted(trusted):
        try:
            VerifyKey(bytes.fromhex(key_hex)).verify(signed, sig)
        except Exception:
            continue
        verified_by = key_hex
        break
    if verified_by is None:
        hint = header.get("signer_pub", "none")
        raise TamperError(
            f"Pack {name!r}: NOT SIGNED BY A TRUSTED KEY - refusing to install. "
            f"It claims signer {hint} (a hint carried inside the pack, never "
            f"used to verify it); trusted here: {', '.join(sorted(trusted))}. "
            "A valid signature only proves the pack is self-consistent - "
            "anyone can mint a key and sign anything. To install a third-party "
            "pack, get the author's public key over a channel you trust and "
            "name it deliberately: read_pack(blob, trusted_keys=[\"<64-hex>\"]) "
            "or install_pack(vault, blob, trusted_keys=[\"<64-hex>\"]). "
            "See PACKS.md.")
    header["verified_by"] = verified_by
    # 2) content hash of the body as stored
    if crypto.sha256(body) != header["content_sha256"]:
        raise TamperError(f"Pack {header['name']!r}: content hash mismatch - "
                          "the pack body was modified; refusing to install")
    # 3) optional decryption
    if header.get("encrypted"):
        if passphrase is None:
            raise PackError(f"Pack {header['name']!r} is encrypted; passphrase required")
        key = crypto.derive_key(passphrase.encode(), bytes.fromhex(header["salt"]),
                                header["kdf"])
        body = crypto.unseal_any(key, body, *wire.pack(header["name"]))

    sections = unpack_sections(body)
    records = [json.loads(l) for l in
               sections["records"].decode("utf-8").splitlines() if l.strip()]
    dim = int(header["model"]["dim"])
    vectors = np.frombuffer(sections["vectors"], dtype=np.float32).reshape(-1, dim)
    if vectors.shape[0] != len(records) or vectors.shape[0] != header["records"]:
        raise TamperError(f"Pack {header['name']!r}: record/vector count mismatch")
    return header, records, vectors


# ---------------------------------------------------------------------------
# Seed (starter memories → ordinary editable records in "main")
# ---------------------------------------------------------------------------

def seed_records(vault, blob: bytes, caller: str = "user",
                 namespace: str | None = None, *,
                 trusted_keys: str | list[str] | None = None) -> dict:
    """Add a pack's contents as ORDINARY memories - the starter path.

    The .mpack file is only the delivery container: its signature and content
    hash are verified before a single record is accepted, exactly like
    install_pack. But nothing lands in a separate read-only section - every
    record goes into the caller's normal namespace (default "main"), fully
    editable and forgettable, as if the agent had stored it. Matching model
    → precomputed vectors load directly (no compute)."""
    header, records, vectors = read_pack(blob, trusted_keys=trusted_keys)
    ns = namespace or vault.config.default_namespace(caller)
    model_matches = (header["model"]["name"] == vault.header.model["name"]
                     and header["model"]["sha256"] == vault.header.model["sha256"])
    if not model_matches:
        vectors = vault.embedder.embed_passages([r["text"] for r in records])
    for r, vec in zip(records, vectors):
        # preserve the author's stable record id as an "id:" tag
        tags = list(r.get("tags", []))
        if "id" in r:
            tags.append(f"id:{r['id']}")
        vault.store(r["text"], caller=caller, namespace=ns,
                    tags=tags, importance=r.get("importance", 0.5),
                    quarantined=False, vec=np.asarray(vec),
                    prov={"host": "seed", "agent": header["creator"],
                          "session": f"{header['name']}@{header['version']}"},
                    # Curated content installs verbatim; the shape gate is
                    # for text being authored, same as _dedup here.
                    _journal=False, _dedup=False, _gate=False)
    vault._audit_and_capture(
        caller, "seed",
        f"{header['name']}@{header['version']}: {len(records)} starting "
        f"memories → {ns}")
    vault.save()
    return {"name": header["name"], "version": header["version"],
            "records": len(records), "namespace": ns,
            "used_precomputed_vectors": model_matches}


# ---------------------------------------------------------------------------
# Install / remove (operate on an unlocked Vault)
# ---------------------------------------------------------------------------

def install_pack(vault, blob: bytes, caller: str = "user",
                 passphrase: str | None = None,
                 allow_reembed: bool = False, *,
                 trusted_keys: str | list[str] | None = None) -> dict:
    header, records, vectors = read_pack(blob, passphrase,
                                         trusted_keys=trusted_keys)
    name = header["name"]
    ns = f"packs/{name}"
    registry = json.loads(vault.db.get_meta("packs", "{}"))
    if name in registry:
        remove_pack(vault, name, caller=caller, _save=False)
        registry = json.loads(vault.db.get_meta("packs", "{}"))

    model_matches = (header["model"]["name"] == vault.header.model["name"]
                     and header["model"]["sha256"] == vault.header.model["sha256"])
    if not model_matches:
        if not allow_reembed:
            raise PackError(
                f"Pack {name!r} was embedded with model {header['model']['name']!r} "
                f"but this vault uses {vault.header.model['name']!r}. "
                "Re-run with --re-embed to re-embed locally (fully offline).")
        vectors = vault.embedder.embed_passages([r["text"] for r in records])

    for r, vec in zip(records, vectors):
        # preserve the pack-author's stable record id as an "id:" tag
        tags = list(r.get("tags", []))
        if "id" in r:
            tags.append(f"id:{r['id']}")
        vault.store(r["text"], caller=caller, namespace=ns,
                    tags=tags, importance=r.get("importance", 0.5),
                    quarantined=False, pack=name, vec=np.asarray(vec),
                    prov={"host": "pack", "agent": header["creator"],
                          "session": f"{name}@{header['version']}"},
                    _journal=False)
    registry[name] = {"version": header["version"], "records": len(records),
                      # the key that actually verified it, not the pack's claim
                      "signer": header["verified_by"][:16],
                      "creator": header["creator"],
                      "description": header.get("description", "")}
    vault.db.set_meta("packs", json.dumps(registry))
    vault._audit_and_capture(caller, "pack-install",
                             f"{name}@{header['version']} ({len(records)} records)")
    vault.save()
    return {"name": name, "version": header["version"], "records": len(records),
            "namespace": ns, "used_precomputed_vectors": model_matches}


def remove_pack(vault, name: str, caller: str = "user", _save: bool = True) -> int:
    ns = f"packs/{name}"
    rows = vault.db.conn.execute(
        "SELECT id, ikey FROM records WHERE ns = ?", (ns,)).fetchall()
    if not rows:
        raise PackError(f"Pack {name!r} is not installed")
    for row in rows:
        vault.db.delete(row["id"], shred=False)
        vault.index.remove(row["ikey"])
        vault._id_by_ikey.pop(row["ikey"], None)
    vault.db.conn.execute("VACUUM")
    registry = json.loads(vault.db.get_meta("packs", "{}"))
    registry.pop(name, None)
    vault.db.set_meta("packs", json.dumps(registry))
    vault._audit_and_capture(caller, "pack-remove", f"{name} ({len(rows)} records)")
    if _save:
        vault.save()
    return len(rows)
