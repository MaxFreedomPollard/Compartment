# engRAM Security Model

Honest threat model. Read the "cannot protect against" section too - a
security product that claims everything protects nothing.

## Cryptography

| Purpose | Primitive |
|---|---|
| All encryption at rest | XChaCha20-Poly1305 AEAD (libsodium via PyNaCl) |
| Key derivation | Argon2id (t=3, m=64MiB, p=4; params stored in header) |
| Keyslots | LUKS-style: random 256-bit master key wrapped per slot |
| Recovery | 16 words from a 256-word list = 128 bits, own keyslot |
| Per-record keys | wrapped by master key → crypto-shred on `forget --shred` |
| Signing (vaults + packs) | Ed25519 |
| Integrity | AEAD tags everywhere + hash-chained audit log |

No homemade crypto. AAD binds every ciphertext to its role and vault
(payloads, journal entries by sequence number, keyslots, record bodies by
record id) - ciphertexts cannot be transplanted between contexts.

## What engRAM protects against

- **Stolen disk / stolen laptop (vault at rest):** the vault file is a
  single AEAD-sealed blob under an Argon2id-wrapped key. No plaintext,
  no plaintext index, no temp files, no logs exist on disk - ever (I2).
- **Vault interception in transit:** a locked vault is safe to move over
  any channel. Optional Ed25519 manifest (`lock --sign`) lets the
  recipient verify origin and integrity without any credential.
- **Tampering:** any modified bit fails AEAD authentication loudly.
  Truncating the file, editing the header, splicing journal entries,
  and downgrade-style version games all produce specific errors.
- **Malicious or modified memory packs:** signature + content hash are
  verified before anything is parsed further; failure aborts install.
- **Forensic recovery of deleted memories:** `forget --shred` destroys the
  per-record key, deletes the row + FTS entries, VACUUMs, and rewrites the
  payload - the content is gone from the current vault file and its
  ciphertext history within that file.
- **Cross-agent memory access:** per-caller namespace grants (rw/ro/none);
  `packs/*` immutable for everyone. Run one server instance per host for
  boundary enforcement (see limitations).
- **Embedding inversion:** vectors can be partially inverted back toward
  text, so engRAM encrypts vectors like everything else. No plaintext
  vector index ever exists on disk.
- **Stored prompt injection:** memories recalled from storage are wrapped
  with a data-not-instructions notice; content stored from untrusted
  sources can be flagged `quarantined`, which attaches an explicit warning
  envelope to every future recall. This is a mitigation, not a guarantee -
  the host agent must still treat memory as data.
- **History falsification:** the audit log is hash-chained; `engram audit
  verify` reports the first broken link.

## What engRAM CANNOT protect against

- **A compromised OS while the vault is unlocked.** Anything that can read
  this process's RAM can read the working set and the master key. This is
  true of every encryption product; unlock only on machines you trust.
- **RAM at unlock time generally** - including swap in pathological cases.
  Python cannot guarantee zeroization (the GC may copy buffers); `lock`
  wipes what it can, best-effort. A future Rust core would tighten this.
- **Pre-shred backups.** Crypto-shred removes content from the *current*
  vault. Copies made before the shred still contain it (encrypted).
- **A hostile host agent within its granted namespaces.** The `--caller`
  identity is declarative. A host that lies about its name gets that
  name's grants. For real isolation, run one `engram serve` per host with
  its own config file and OS-level separation.
- **Weak passphrases.** Argon2id slows attackers; it cannot save
  "password1". The passphrase is user-chosen and is the ONLY credential -
  engRAM never generates a password or recovery seed, so there is nothing
  written down anywhere unless you write it. Losing the passphrase (and
  the 2FA keyfile, if enrolled) makes the vault permanently
  unrecoverable; that is the design, not a failure mode. To harden a
  human-memorable passphrase, enable two-factor unlock: `engram 2fa
  enable` wraps the master key under Argon2id(passphrase ‖ keyfile), so
  both the thing you know and the file you hold are required - a
  cryptographic requirement, not a prompt that malware can skip. (Why not
  authenticator-app codes? A six-digit TOTP cannot cryptographically
  protect an offline file: the verifying secret would have to live next
  to the data it guards, so any code-based gate on a local vault is
  theater. The keyfile is the honest second factor for this threat
  model.)
- **Search-audit persistence gap:** search audit entries are held in RAM
  until the next save/lock (writes are journaled immediately; reads don't
  cost an fsync). A kill -9 can lose recent *search* audit entries - never
  store/forget entries.

## Unlock paths, ranked

1. **Boot-session credential (the default).** `engram unlock` wraps the
   master key with a key derived from the current boot's kernel timestamp
   (plus uid + hostname) and stores it 0600 in `~/.engram/session/`. The
   vault then stays continuously usable - across processes, logouts, and
   logins, for weeks or months - and RELOCKS on any restart or power loss:
   the new boot's derivation can never open the old wrap, and stale files
   are deleted on sight. This is deliberately a convenience credential -
   an attacker who can read it on the *running* machine could also read
   process RAM; once power is lost it is dead ciphertext. (A forensic
   caveat: the previous boot time may be recoverable from system logs, so
   on an unencrypted disk a stolen *file pair* is theoretically weaker
   than the passphrase. Use FileVault/FDE, which you should anyway.)
2. **macOS Keychain** (`engram unlock --keychain`, explicit opt-in):
   credential guarded by the OS keychain. Stronger against file theft than
   the session credential, but it SURVIVES REBOOTS - choose it only if
   that is what you want.
3. **`ENGRAM_PASSPHRASE` env var:** for scripts/CI; visible to anything
   that can read the process environment.
4. **`memory_unlock` MCP tool: DISABLED by default.** The passphrase would
   transit the agent's context window and possibly the host's logs/model
   provider. Enable only if you accept that
   (`settings.unlock_tool_enabled` in `<vault>.config.json`).

`engram lock` and the `memory_lock` panic tool clear ALL stored
credentials (session + keychain). Auto-lock drops the in-RAM key after
`auto_lock_minutes` (default 30) idle; while a stored credential remains,
the next operation silently re-opens - stored credentials represent
standing user intent, ended by `engram lock` or a reboot.

**Two-factor interaction, stated honestly:** with `engram 2fa enable`,
every *credential-based* unlock (passphrase paths 3-4 above, and any
fresh `engram unlock`) requires the keyfile too - that is enforced by
the KDF. The session and keychain paths store the unwrapped MASTER key,
so after one successful two-factor unlock they re-open the vault without
re-presenting the keyfile, exactly as they skip the passphrase. That is
the design (they encode standing intent on an already-trusted machine);
if you want every single open to need both factors, run `engram unlock
--once` and don't store a credential.

## Multi-agent, one vault

Several agent processes (Hermes provider, Claude via MCP, the CLI) may
share one vault: an advisory file lock serializes every journal append and
save, and each process detects foreign writes (mtime/size) and reloads
before proceeding - a stale writer gets a loud VaultStaleError, never
silent corruption. Namespace ACLs are per-caller; `--caller` identity is
declarative (see the hostile-host limitation above).

## Reporting

Report vulnerabilities privately to the maintainer. No telemetry exists in
this product; nothing phones home, so nothing can be recalled remotely.
