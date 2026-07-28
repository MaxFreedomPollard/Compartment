# Compartment On-Disk Formats (v1)

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
…       *     journal entries, each: u32 len | u32 crc32(len) | ciphertext
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
  "extra":       {"creator": "...", "wire": 2}
}
```

The header is plaintext by design: it contains only public parameters
(never secrets), and must be readable to attempt unlock. Its integrity is
enforced indirectly: every ciphertext binds the `vault_id` in its AAD, and
keyslot wrapping fails AEAD auth if slots are altered.

`extra.wire` is the spelling of the AAD labels in this file, absent meaning 1:

| | labels |
|---|---|
| 1 | `engram-keyslot`, `engram-payload:`, `engram-record:`, `engram-record-body:`, `engram-journal:`, `engram-pack:`, `engram-session:`, and `\x1f engram-2fa \x1f` |
| 2 | the `compartment-` spellings used throughout this document |

Both are readable. A reader tries the current label and falls back to the
legacy one; a writer only ever emits the current label. Opening a `wire: 1`
vault therefore rewrites it as `wire: 2` in place, which is a relabelling and
not a re-encryption: the master key, the per-record keys and every plaintext
are unchanged. An implementation that drops the legacy labels cannot open any
vault written before 2.2.

### KeySlot

```json
{"type": "passphrase" | "recovery" | "passphrase+keyfile",
 "kdf":  {"alg": "argon2id", "time_cost": 3, "memory_kib": 65536, "parallelism": 4},
 "salt": "16-byte hex",
 "keyfile_id": "first 16 hex chars of SHA-256(keyfile)   (passphrase+keyfile only)",
 "wrapped": "hex of AEAD_seal(key=Argon2id(secret, salt), msg=master_key,
             aad='compartment-keyslot')"}
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
  `utf8(passphrase) ‖ b"\x1f compartment-2fa \x1f" ‖ keyfile_bytes`, so both
  factors feed the KDF; `keyfile_id` exists only to name the wrong file
  in error messages (the keyfile is ≥16 random bytes, not guessable).

### Payload

`payload_plain = TLV(sections)`; currently one section, `"sqlite"` - a
serialized SQLite database image (schema in `store.py`; includes records,
FTS5 index, audit chain, meta).
`payload_ct = AEAD_seal(master_key, payload_plain, aad="compartment-payload:"+vault_id)`.

TLV container:
```
u32 section_count, then per section:
  u16 name_len | name UTF-8 | u64 data_len | data
```

### Journal

Each entry: `AEAD_seal(master_key, canonical_json(entry),
aad="compartment-journal:"+vault_id+":"+u64_be(seq))` where `seq` starts at 0
after each compaction and increments per entry. Entries are fsync'd on
append (acknowledged). Ops:

```json
{"op":"store","audit":AuditRow,"record":{"id","ns","text","vec":base64_f32,
  "tags",[...],"importance",n,"quarantined",b,"pack",s|null,"prov",{...},"created",t}}
{"op":"forget","id":"...","shred":true|false,"audit":AuditRow}
```

Each entry is framed `u32 len | u32 crc32(len) | ciphertext`, where the
checksum covers the four length bytes only. It exists to separate two
failures that are otherwise identical from the outside, because the length is
the only thing that says where the next entry begins:

- **intact prefix, short body, nothing after it** - the process died mid
  append. The entry was never acknowledged, so it is discarded with a notice
  and nothing confirmed is lost.
- **prefix fails its own checksum** - corruption. Entries after it cannot be
  located, so the reader aborts with a tamper error rather than treating the
  remainder of the journal as a crash artifact and discarding it. A torn
  write never produces this: an interrupted append leaves the prefix it
  already wrote intact.

A declared length beyond 64 MiB is a tamper error even with a valid checksum.
Any AEAD failure on a complete entry is a tamper error: abort.

The audit chain's head hash and length are also pinned in the payload's meta
table (`audit_anchor_head`, `audit_anchor_count`) at creation and on every
save. A forward walk of a hash chain cannot detect its own tail being cut
off, since what remains is still internally consistent, so verification
requires the chain to extend the anchor. The anchor is mandatory: its absence
is reported as tampering, not as an older vault.

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

The verifying key comes from the reader's TRUSTED SET, never from the pack.
`signer_pub` in the header is an identifying hint only: a pack that supplied
its own verification key would be vouching for itself, which proves nothing,
since anyone can mint a keypair and sign anything. A conforming reader ships
the keys it trusts and refuses everything else unless an operator names a key
deliberately.

**Verification order on install: signature (against a trusted key) → content
hash → (decrypt) → parse.** Any failure aborts before further parsing.

Body (after optional AEAD unseal with
`aad="compartment-pack:"+name`): TLV sections
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
