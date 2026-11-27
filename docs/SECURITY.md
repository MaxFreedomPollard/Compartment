# Compartment Security Model

Honest threat model for Compartment, covering both what it defends against and its hard limits. Read the bottom section too. Compartment is designed to provide the strongest practical local security of any AI memory and document vault: persistent memory and documents hold your private context, decisions, notes, and files, making them a high-value target.

Compartment runs **100% offline, with zero network calls of any kind**, and stores all memories, documents, versions, relations, and embeddings inside a single portable, encrypted `.vault` file you own and can back up or copy anywhere (local disk, USB, NAS, or any cloud drive) like any other file.

## What Compartment defends against

- **Stolen disk, stolen laptop, or exposed backups (vault and documents at rest):**
  Everything lives inside one `FORMAT.md` v1 `.vault` file sealed with XChaCha20-Poly1305 AEAD under a random 256-bit master key wrapped with Argon2id (`t=3, m=64 MiB, p=4`). Atomic memories, persistent documents (every immutable version and chunk), relations, FTS5 indexes, and embedding vectors are all encrypted together. No plaintext file, plaintext vector index, temp file, or log ever exists on disk (Invariant I2). No recovery phrase or hidden backdoor credential is generated; only your passphrase (and optional 2FA keyfile) opens the vault.
- **Reboot and stolen session-credential theft (volatile kernel secret):**
  `compartment unlock` wraps the master key under HMAC-SHA256 keyed by a random 32-byte per-boot secret plus boot timestamp, UID, and machine ID, stored mode `0600` in `~/.compartment/session/`. The 32-byte secret lives strictly in a volatile kernel object (POSIX shared memory via `shm_open` on macOS/Linux, a volatile registry key on Windows) and is never written to any filesystem. A reboot (or Windows logoff) destroys it automatically, so a stolen disk image, snapshot, backup, or credential file on its own opens nothing. Where no volatile kernel holder is available, storing a session credential is refused rather than degrading to predictable values.
- **Offline brute force, online passphrase guessing, and argv leaks:**
  Argon2id slows offline guessing. Failed online passphrase attempts are paced with a flat 5-second interval (deliberately flat with no escalating lockout, so an attacker cannot lock the vault owner out of an unrecoverable file). Passphrases from the apps and CLI travel over `stdin` (`--passphrase-stdin`), never `argv` (never exposed in process listings), and UI passphrase boxes clear immediately on submit. Optional cryptographic two-factor unlock (`compartment 2fa enable`) mixes passphrase and keyfile directly into Argon2id (`Argon2id(passphrase || keyfile)`), making the second factor a mathematical KDF requirement rather than a bypassable prompt.
- **Network exfiltration, silent phone-home, and runtime telemetry:**
  Zero network calls of any kind at runtime and zero listening network ports (the MCP server speaks stdio only; the optional desktop GUI opens zero sockets). A runtime offline guard (`--assert-offline` / `COMPARTMENT_ASSERT_OFFLINE=1`), backed by Python audit hooks (`sys.addaudithook`) and socket/DNS interception, aborts on any network socket or non-local DNS attempt and runs across the test suite in CI on macOS, Linux, and Windows. Compartment also sets `ORT_DISABLE_TELEMETRY=1` before loading ONNX Runtime so the local embedding engine sends nothing.
- **Embedding inversion attacks:**
  Dense embeddings can be partially inverted back toward source text. Compartment encrypts all memory window vectors and document chunk vectors inside the vault alongside the prose; no plaintext vector index ever touches disk.
- **Tampering, truncation, splicing, downgrade, and ciphertext transplantation:**
  Every layer carries AEAD authentication tags with strict domain-separated Associated Data (AAD) binding each ciphertext to its exact role and identity (payload, vault ID, journal sequence number, keyslot, record ID, document version ID, and document chunk ID). Modified bits, truncated files, malformed keyslots/salts, spliced journal entries, and cross-context ciphertext swaps fail loudly with specific errors. Optional Ed25519 vault signing (`lock --sign`) lets any recipient verify origin and integrity (`compartment verify`) without any passphrase.
- **Backup corruption and plaintext leaks during backup or multi-machine merge:**
  Encrypted backups (`compartment backup` / **Back up…**) work locked or unlocked without prompting for a passphrase or writing plaintext, strip any unacknowledged crashed journal tail, write atomically at mode `0600` with `fsync`, and re-parse plus SHA-256 verify the result. Multi-machine merges (`compartment merge` / **Merge…**) decrypt and reconcile memories, relations, and documents strictly in RAM without writing plaintext to disk.
- **Forensic recovery of deleted memories and documents (crypto-shred):**
  Every memory record has its own wrapped per-record key and every document version has its own wrapped 256-bit data key. `forget --shred` and `doc delete --shred` (for an entire document or one historical version) destroy those wrapped keys, delete the rows, FTS entries, and chunk vectors, `VACUUM` the in-memory SQLite image, and atomically rewrite the sealed payload.
- **Path traversal and unsafe document exports:**
  Document paths reject `..`, `.`, drive letters, NUL bytes, and control characters before entering the vault, and `doc export-all` verifies every resolved output path stays strictly inside the target destination directory.
- **Malicious or forged memory packs:**
  Pack Ed25519 signatures are verified strictly against a trusted key, never against the `signer_pub` embedded in the pack itself. Only the project key ships as trusted; third-party keys must be named explicitly (`compartment pack install --trusted-key HEX`). Verification runs strictly in order (signature -> content hash -> decrypt -> parse) and aborts on any failure.
- **Cross-agent leakage, concurrent corruption, and stored prompt injection:**
  Per-caller namespace ACLs enforce `rw`, `ro`, or `none` access; `packs/*` is immutable for every caller. Advisory file locking serializes writes across concurrent processes and detects foreign disk writes loudly (`VaultStaleError`) instead of silently overwriting them. All recalled memories and document excerpts are wrapped with a data-not-instructions envelope; untrusted records/documents can be flagged `quarantined` for an explicit warning on every recall; `memory_unlock` over MCP is disabled by default so passphrases never cross an LLM context window; and agent rule integrations stay strictly additive to host instruction/skill files so recalled memory data never replaces trusted instructions. Grounding atomic memories in a source document (`doc_id`, `doc_version`) automatically flags older derived claims `doc_stale: true` whenever the source document changes, archives, or is deleted.
- **Local UI, helper, dashboard, and embedding-daemon exposure:**
  - The menu bar / tray helper (`compartment.vaultview`) communicates solely over private stdin/stdout pipes (no socket, no port), opens the vault strictly read-only, exits after 10 minutes idle, and drops the master key within 30 seconds of an external `compartment lock`.
  - The optional desktop GUI (`compartment gui`) opens zero sockets, clears passphrase entries immediately, watches for external locks every 15 seconds to drop its in-RAM key automatically, and drops its key when closed.
  - The local browser dashboard (`compartment dash`) binds `127.0.0.1` only on an ephemeral port, gates access behind a constant-time-compared random URL token (`404` without it), is strictly read-only (`GET` only), sets `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and strict `Content-Security-Policy: default-src 'none' ...`, and stops automatically on `Lock`.
  - The shared embedding daemon (`embed_daemon`) never opens a vault and holds no keys; its Unix domain socket sits inside the mode-`0700` session directory, enforces same-UID peer credentials (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS), and validates exact package version and model SHA-256.
- **History falsification and binary / CI supply-chain tampering:**
  The audit log is hash-chained so `compartment audit verify` pinpoints any deleted, altered, or reordered entry. macOS releases are signed deepest-path-first with Apple Developer ID certificates under Hardened Runtime (with Library Validation intact across every `.dylib`, `.so`, embedded Python runtime, and native launcher), Apple notarized (`xcrun notarytool`), stapled (`xcrun stapler`), and shipped with `SHA256SUMS.txt` (plus optional macOS App Sandbox packaging). Release and CI GitHub Actions workflows pin third-party actions to full commit SHAs.

---

## Cryptography

| Purpose | Primitive |
|---|---|
| All encryption at rest | XChaCha20-Poly1305 AEAD (libsodium via PyNaCl) |
| Key derivation | Argon2id (`t=3`, `m=64 MiB`, `p=4`; params stored in header) |
| Keyslots | LUKS-style: random 256-bit master key wrapped per slot |
| Recovery | None. Your passphrase (plus optional 2FA keyfile) is the only credential |
| Per-record keys | Wrapped by master key -> crypto-shred on `forget --shred` |
| Per-document-version keys | Wrapped by master key -> crypto-shred on `doc delete --shred` |
| Signing (vaults + packs) | Ed25519 |
| Integrity | AEAD tags everywhere + hash-chained audit log |

Vaults created before 1.8.0 were issued a 16-word recovery phrase in their own keyslot. Those still open with that phrase, but no new vault is given one.

No homemade cryptography is used. Domain-separated AAD binds every ciphertext to its role and vault (`payload`, `journal` entry sequence number, `keyslot`, `record-key`, `record-body`, `doc-key`, `doc-body`, `doc-chunk`, `pack`) so ciphertexts cannot be transplanted between contexts.

## Unlock paths, ranked

1. **Boot-session credential (default):** `compartment unlock` wraps the master key under HMAC-SHA256 keyed by the random 32-byte volatile kernel secret (POSIX `shm_open` on macOS/Linux, volatile registry key on Windows) plus boot timestamp, UID, and machine ID, stored mode `0600` in `~/.compartment/session/`. A reboot (or Windows logoff) destroys the kernel secret and forces a fresh unlock. It is a convenience credential on an already-trusted live session, not a second factor.
2. **macOS Keychain (`compartment unlock --keychain`, explicit opt-in):** Credential guarded by the OS keychain. Stronger against raw file theft than the session file, but it survives reboots; enable it only if you want unlock to persist across restarts.
3. **`COMPARTMENT_PASSPHRASE` environment variable:** For scripts and CI; visible to anything that can inspect the process environment.
4. **`memory_unlock` MCP tool (DISABLED by default):** Passing a passphrase through MCP sends it across the agent's context window and potentially host logs or model providers. Enable only if you explicitly accept that tradeoff (`settings.unlock_tool_enabled` in `<vault>.config.json`).

`compartment lock` and the `memory_lock` panic tool clear all stored credentials (session + keychain) and end running panel helpers/dashboards/GUIs. Auto-lock drops the in-RAM key after `auto_lock_minutes` (default 30) idle; while a stored credential remains, the next operation silently re-opens (stored credentials represent standing user intent until `compartment lock` or a reboot).

**Two-factor interaction, stated honestly:** With `compartment 2fa enable`, every passphrase-based unlock requires the keyfile too (enforced by Argon2id). Once unlocked, the session and keychain credentials hold the unwrapped master key so subsequent calls re-open without re-presenting the keyfile, just as they skip the passphrase. If you want every single open to require both factors, use `compartment unlock --once` and store no credential.

## Process and UI isolation

- **Multi-agent, one vault:** Concurrent agent processes (Claude, Codex, Hermes, AntiGravity, OpenClaw, CLI, GUI) serialize journal appends and saves under an advisory file lock and detect foreign writes by inode/size/mtime before mutating.
- **Top-bar / tray reader (`compartment.vaultview`):** Runs as one read-only child talking JSON lines over private stdin/stdout pipes (no socket, no port), serves both snapshots and document search/preview without writing to the vault or audit log, exits after 10 idle minutes, and drops its key within 30 seconds of an external lock.
- **Optional desktop GUI (`compartment gui`):** Runs locally over standard-library `tkinter`/`ttk` with zero sockets, drops its key on window close or external lock detection (polled every 15 seconds), and only writes plaintext to disk on explicit user export.
- **Shared embedding daemon (`embed_daemon`):** Runs one shared local ONNX process (~70 MB) over a mode-`0700` Unix domain socket (macOS/Linux; Windows keeps the model in-process). Only query/passage text goes in and unit vectors come out; no passphrase, master key, record key, or vault data ever crosses it. Disable per-vault (`embed_daemon: false`) or via `COMPARTMENT_EMBED_DAEMON=0`.

---

## What Compartment cannot defend against

Be clear-eyed about these hard boundaries:

- **A compromised OS or malicious process running as your user while the vault is unlocked:**
  Anything with permission to inspect your live user processes or volatile kernel memory can read the working set, the per-boot session secret, and the master key. This is true of every local encryption tool; unlock only on machines you trust.
- **RAM exposure at unlock time (including swap, hibernation, and Python GC):**
  A live RAM capture, hibernation image, suspended VM snapshot, or swap/pagefile copy made while unlocked can expose plaintext or keys. Python cannot guarantee deterministic zeroization because the garbage collector may copy buffers; `lock` clears references and wipes mutable buffers on a best-effort basis.
- **Pre-shred backups and copies:**
  `forget --shred` and `doc delete --shred` remove content and keys from the *current* vault file. Copies or backups made before the shred still retain that earlier encrypted state.
- **A hostile host agent within its granted namespaces:**
  The `--caller` identity is declarative. A host process that lies about its `--caller` name receives that caller's namespace grants. For real multi-tenant isolation, run separate `compartment serve` instances with separate config files and OS-level user/container separation.
- **Host agents ignoring the data-not-instructions boundary:**
  Compartment wraps recalled memories and documents as untrusted data and supports `quarantined` warnings, but preventing prompt injection ultimately depends on the host LLM/agent honoring that boundary.
- **Weak passphrases and forgotten credentials:**
  Argon2id slows brute force, but it cannot rescue `"password1"`. Because Compartment intentionally creates no recovery phrase or backdoor, forgetting your passphrase (or losing your 2FA keyfile if enabled) makes the vault permanently unrecoverable. (TOTP authenticator codes are intentionally not used as an offline second factor because a local verifying secret next to the vault would be security theater; a high-entropy keyfile mixed into Argon2id is the honest cryptographic second factor.)
- **Search-audit persistence gap on `kill -9`:**
  Write operations (`store`, `forget`, `link`, `doc_put`, `doc_patch`, `doc_delete`) are journaled immediately. Read/search audit entries stay in RAM until the next save or lock so reads do not cost an `fsync`; an abrupt `kill -9` can lose recent *search* audit rows, never write entries.

## Reporting

Report security vulnerabilities privately to the maintainer (via GitHub Private Vulnerability Reporting on the repository Security tab). Because Compartment includes zero telemetry and makes zero network calls, nothing phones home and nothing can be recalled remotely.
