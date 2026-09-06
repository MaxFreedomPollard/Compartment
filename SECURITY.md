# Compartment Security Model

Honest threat model. Read the "cannot protect against" section too. I can honestly say Compartment has the best security of any vector memory. Important, as vector memory literally holds the entire computer's memory and essentially all chat logs + everything else from all operations. Vector memory is the #1 target for hackers, because of its high value and (until now) total lack of any security.

## Cryptography

| Purpose | Primitive |
|---|---|
| All encryption at rest | XChaCha20-Poly1305 AEAD (libsodium via PyNaCl) |
| Key derivation | Argon2id (t=3, m=64MiB, p=4; params stored in header) |
| Keyslots | LUKS-style: random 256-bit master key wrapped per slot |
| Recovery | none. Your passphrase is the only credential; nothing is generated for you |
| Per-record keys | wrapped by master key → crypto-shred on `forget --shred` |
| Signing (vaults + packs) | Ed25519 |
| Integrity | AEAD tags everywhere + hash-chained audit log |

Vaults created before 1.8.0 were issued a 16-word recovery phrase in their own
keyslot. Those still open with that phrase, but no new vault is given one.

No homemade crypto. AAD binds every ciphertext to its role and vault
(payloads, journal entries by sequence number, keyslots, record bodies by
record id) - ciphertexts cannot be transplanted between contexts.

## What Compartment protects against

- **Stolen disk / stolen laptop (vault at rest):** the vault file is a
  single AEAD-sealed blob under an Argon2id-wrapped key. No plaintext,
  no plaintext index, no temp files, no logs exist on disk - ever (I2).
- **Vault interception in transit:** a locked vault is safe to move over
  any channel. Optional Ed25519 manifest (`lock --sign`) lets the
  recipient verify origin and integrity without any credential.
- **Tampering:** any modified bit fails AEAD authentication loudly.
  Truncating the file, editing the header, splicing journal entries,
  and downgrade-style version games all produce specific errors.
- **Malicious or modified memory packs:** the Ed25519 signature is checked
  against a TRUSTED key, never against the `signer_pub` the pack carries.
  A pack cannot vouch for itself: anyone can mint a keypair and sign
  anything, so a self-consistent signature proves nothing about authorship.
  Only the project key ships as trusted; a third party key has to be named
  deliberately (`compartment pack install --trusted-key HEX`). Signature,
  then content hash, then decrypt, then parse. Failure aborts install.
- **Forensic recovery of deleted memories:** `forget --shred` destroys the
  per-record key, deletes the row + FTS entries, VACUUMs, and rewrites the
  payload - the content is gone from the current vault file and its
  ciphertext history within that file.
- **Cross-agent memory access:** per-caller namespace grants (rw/ro/none);
  `packs/*` immutable for everyone. Run one server instance per host for
  boundary enforcement (see limitations).
- **Embedding inversion:** vectors can be partially inverted back toward
  text, so Compartment encrypts vectors like everything else. No plaintext
  vector index ever exists on disk.
- **Stored prompt injection:** memories recalled from storage are wrapped
  with a data-not-instructions notice; content stored from untrusted
  sources can be flagged `quarantined`, which attaches an explicit warning
  envelope to every future recall. This is a mitigation, not a guarantee -
  the host agent must still treat memory as data.
- **History falsification:** the audit log is hash-chained; `compartment audit
  verify` reports the first broken link.

## What Compartment CANNOT protect against

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
  name's grants. For real isolation, run one `compartment serve` per host with
  its own config file and OS-level separation.
- **Weak passphrases.** Argon2id slows attackers; it cannot save
  "password1". The passphrase is user-chosen and is the ONLY credential -
  Compartment never generates a password or recovery seed, so there is nothing
  written down anywhere unless you write it. Losing the passphrase (and
  the 2FA keyfile, if enrolled) makes the vault permanently
  unrecoverable; that is the design, not a failure mode. To harden a
  human-memorable passphrase, enable two-factor unlock: `compartment 2fa
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

1. **Boot-session credential (the default).** `compartment unlock` wraps the
   master key under HMAC-SHA256 keyed by a random 32-byte per-boot secret,
   together with the boot timestamp, uid and machine id, and stores it 0600
   in `~/.compartment/session/`. The secret is held in a volatile kernel
   object - a POSIX shared-memory segment on macOS and Linux, a volatile
   registry key on Windows - and is never written to any filesystem, so a
   backup, snapshot or disk image does not contain it. A restart destroys it,
   which is what makes the relock real rather than a policy. On Windows a
   full logoff relocks as well. A stolen *file* on its own is therefore
   useless: guessing the boot time, uid and machine id still leaves 32
   unknown bytes. It remains a convenience credential - anything running as
   you on the live machine reads the same secret, and could read the master
   key out of process RAM anyway, and a RAM capture (hibernation image,
   suspended VM, swap or pagefile) can expose it too. It is not a second
   factor. Where no volatile holder exists, storing a credential is REFUSED
   rather than falling back to values an attacker would already hold.
2. **macOS Keychain** (`compartment unlock --keychain`, explicit opt-in):
   credential guarded by the OS keychain. Stronger against file theft than
   the session credential, but it SURVIVES REBOOTS - choose it only if
   that is what you want.
3. **`COMPARTMENT_PASSPHRASE` env var:** for scripts/CI; visible to anything
   that can read the process environment.
4. **`memory_unlock` MCP tool: DISABLED by default.** The passphrase would
   transit the agent's context window and possibly the host's logs/model
   provider. Enable only if you accept that
   (`settings.unlock_tool_enabled` in `<vault>.config.json`).

`compartment lock` and the `memory_lock` panic tool clear ALL stored
credentials (session + keychain). Auto-lock drops the in-RAM key after
`auto_lock_minutes` (default 30) idle; while a stored credential remains,
the next operation silently re-opens - stored credentials represent
standing user intent, ended by `compartment lock` or a reboot.

**Two-factor interaction, stated honestly:** with `compartment 2fa enable`,
every *credential-based* unlock (passphrase paths 3-4 above, and any
fresh `compartment unlock`) requires the keyfile too - that is enforced by
the KDF. The session and keychain paths store the unwrapped MASTER key,
so after one successful two-factor unlock they re-open the vault without
re-presenting the keyfile, exactly as they skip the passphrase. That is
the design (they encode standing intent on an already-trusted machine);
if you want every single open to need both factors, run `compartment unlock
--once` and don't store a credential.

## Multi-agent, one vault

Several agent processes (Hermes provider, Claude via MCP, the CLI) may
share one vault: an advisory file lock serializes every journal append and
save, and each process detects foreign writes (mtime/size) and reloads
before proceeding - a stale writer gets a loud VaultStaleError, never
silent corruption. Namespace ACLs are per-caller; `--caller` identity is
declarative (see the hostile-host limitation above).

## The shared embedding process

Since 4.9.6 the encoder runs once per machine, in a daemon that every
`compartment serve`, the command line and the app talk to over a Unix domain
socket, rather than once per agent. What crosses that socket: the text of a
memory being stored or of a query being searched, going in; unit vectors
coming out. What never crosses it: a passphrase, a master key, a record key,
or anything read from the vault at rest. The daemon opens no vault and holds
no key; a capture of its memory is a copy of the model and of whatever text
was in flight.

The socket lives in the session directory (`~/.compartment/session`, mode
0700) beside the boot-bound unlock credential. The daemon accepts a
connection only from a process with its own uid (`SO_PEERCRED` on Linux,
`LOCAL_PEERCRED` on macOS); a client connects only to a socket file its own
uid owns, and only to a daemon that reports the same protocol, the same
package version and the same SHA-256 of the model file, so a vector never
comes from a model other than the one the vault is pinned to. A process
running as the same user is already inside the trust boundary - it can read
the session credential and open the vault directly - so the socket gives
that user nothing they did not have. It is not a network listener:
`--assert-offline` allows AF_UNIX and forbids everything else, exactly as
before.

`COMPARTMENT_EMBED_DAEMON=0`, or `embed_daemon: false` in the vault's
settings, keeps the model inside each process instead. Windows has no
AF_UNIX in Python and always does.

## Reporting

Report vulnerabilities privately to the maintainer. No telemetry exists in
this product; nothing phones home, so nothing can be recalled remotely.
