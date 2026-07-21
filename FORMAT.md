# engRAM On-Disk Formats (v1)

Language-agnostic byte-level spec. A conforming implementation in any
language can read both formats from this document alone (invariant I5).
All integers are big-endian. All JSON is UTF-8; where signed/hashed it is
*canonical*: sorted keys, separators `,` and `:`, no whitespace.

## 1. Vault file (`.vault`)

```
offset  size  field
0       4     magic = "NUCV"
4       2     format_version = 0x0001
6       4     header_len (u32)
10      N     header JSON (plaintext, canonical at write time)
10+N    P     payload ciphertext   (P = header.payload_len)
…       *     journal entries, each: u32 len | entry ciphertext
```

### Header JSON

```json
{
  "vault_id":    "32-char hex (uuid4)",
  "created":     "ISO-8601 UTC",
  "keyslots":    [KeySlot, ...],
  "payload_len": 12345,
  "model":       {"name": "...", "sha256": "...", "dim": 384},
  "manifest":    null | Manifest,
  "extra":       {"creator": "..."}
}
```

The header is plaintext by design: it contains only public parameters
(never secrets), and must be readable to attempt unlock. Its integrity is
enforced indirectly: every ciphertext binds the `vault_id` in its AAD, and
keyslot wrapping fails AEAD auth if slots are altered.

### KeySlot

```json
{"type": "passphrase" | "recovery" | "passphrase+keyfile",
 "kdf":  {"alg": "argon2id", "time_cost": 3, "memory_kib": 65536, "parallelism": 4},
 "salt": "16-byte hex",
 "keyfile_id": "first 16 hex chars of SHA-256(keyfile)   (passphrase+keyfile only)",
 "wrapped": "hex of AEAD_seal(key=Argon2id(secret, salt), msg=master_key,
             aad='engram-keyslot')"}
```

`AEAD_seal(key, msg, aad)` = 24-byte random nonce ‖
XChaCha20-Poly1305-IETF(msg, aad, nonce, key).

Slot secrets:
- `passphrase` - the user's passphrase, UTF-8. Since 1.8.0 this is the
  only slot `init`/`rekey` create; no credential is ever auto-generated.
- `recovery` - LEGACY (1.7 and earlier auto-generated it; 1.8+ only
  reads it): the 16 words joined by single spaces, lowercase. The
  wordlist (256 words) is part of this spec (see `crypto.WORDLIST`);
  each word encodes one byte by index.
- `passphrase+keyfile` (two-factor, 1.8.0+) - secret =
  `utf8(passphrase) ‖ b"\x1f engram-2fa \x1f" ‖ keyfile_bytes`, so both
  factors feed the KDF; `keyfile_id` exists only to name the wrong file
  in error messages (the keyfile is ≥16 random bytes, not guessable).

### Payload

`payload_plain = TLV(sections)`; currently one section, `"sqlite"` - a
serialized SQLite database image (schema in `store.py`; includes records,
FTS5 index, audit chain, meta).
`payload_ct = AEAD_seal(master_key, payload_plain, aad="engram-payload:"+vault_id)`.

TLV container:
```
u32 section_count, then per section:
  u16 name_len | name UTF-8 | u64 data_len | data
```

### Journal

Each entry: `AEAD_seal(master_key, canonical_json(entry),
aad="engram-journal:"+vault_id+":"+u64_be(seq))` where `seq` starts at 0
after each compaction and increments per entry. Entries are fsync'd on
append (acknowledged). Ops:

```json
{"op":"store","audit":AuditRow,"record":{"id","ns","text","vec":base64_f32,
  "tags",[...],"importance",n,"quarantined",b,"pack",s|null,"prov",{...},"created",t}}
{"op":"forget","id":"...","shred":true|false,"audit":AuditRow}
```

Reading: a **truncated final** entry (length prefix incomplete or body
short at EOF) is an unacknowledged crash artifact - discard with notice.
Any other malformed entry, or any AEAD failure, is a tamper error: abort.

Compaction (`save`/`lock`): serialize → seal → write `path.tmp` → fsync →
atomic rename; journal restarts empty.

### Manifest (signed vaults)

```json
{"creator":"...","created":"...","vault_id":"...",
 "content_sha256": sha256_hex(payload_ct),
 "signer_pub": ed25519_pub_hex,
 "sig": ed25519_sig_hex_over_canonical_json_of_all_other_fields}
```

Verifiable with zero key material. A signed vault must have an empty
journal (entries after signing = tamper).

## 2. Memory pack (`.mpack`)

```
0   4   magic = "NUCP"
4   2   pack_version = 0x0001
6   4   header_len (u32)
10  N   header JSON
10+N *  body
```

Header:
```json
{"name","version"(semver),"description","creator",
 "model": {"name","sha256","dim"},
 "records": count,
 "encrypted": bool,           // + "kdf","salt" when true
 "content_sha256": sha256_hex(body_as_stored),
 "signer_pub": hex, "sig": hex}
```

`sig` = Ed25519 over canonical JSON of the header minus `sig`. Because the
header pins `content_sha256`, the signature covers the body transitively.
**Verification order on install: signature → content hash → (decrypt) →
parse.** Any failure aborts before further parsing.

Body (after optional AEAD unseal with
`aad="engram-pack:"+name`): TLV sections
`"records"` = JSONL, one `{"id"?, "text", "tags"?, "importance"?}` per line;
`"vectors"` = `records × dim` float32 little-endian, row-major, L2-normalized,
computed with `header.model`.

Install: if the vault's model name+sha256 equal the pack's, load vectors
directly; else re-embed locally (explicit opt-in). Records install
read-only into namespace `packs/<name>`; the author's `id` is preserved as
an `id:<orig>` tag.

## 3. Versioning

`format_version` / `pack_version` bump on any incompatible change; readers
must refuse unknown versions loudly (no best-effort parsing). Additive
header fields are allowed within a version; unknown fields are preserved.
