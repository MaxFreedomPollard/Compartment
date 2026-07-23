# engRAM

### Superior agentic memory, encrypted at rest.

engRAM is the memory your AI agents plug into. Hermes, Claude, and
OpenClaw each register in one command; anything that runs a subprocess
can use the CLI. Every agent can share the same memory - even across
different machines. Everything is local. The default install never
touches the network: the embedding model ships inside the package, so
recall works as soon as the vault is open. Every byte at rest is
AEAD-encrypted, the vectors included. The vault locks itself on restart
or power loss and unlocks once per boot.

*An **engram** is the physical trace a memory leaves in the brain. This
one lives in **RAM**: the whole index is held in memory, which is what
makes it both the fastest place to search and the safest place to keep
plaintext, because nothing decrypted is ever written to disk.*

## Why engRAM

Most memory tools ask you to choose: powerful, or private, or easy. engRAM
refuses the trade, because one design decision delivers all three.

**Better recall.** engRAM does not just store chat, it decides what
matters. A bare "OK" answering "send this reply to the client?" is
captured as a consent decision, with its question, at the highest
priority. What you said about yourself and your machine outranks
background noise. Search is hybrid (meaning plus keywords) and, at
personal scale, mathematically exact: the top result is the true top
result, not an approximation. It learns you first, and forgets nothing.

**More secure, by construction.** Every byte at rest is authenticated-
encrypted, the embedding vectors included (most tools leave those in the
clear, and vectors can be inverted back toward text). Deletion is
cryptographic: destroy the record's key and it is gone, unrecoverable.
Tampering is detected, history is hash-chained, and the vault locks itself
on restart or power loss. It runs fully offline: a runtime guard aborts on
any network attempt, and CI proves it on three operating systems.

**Not one step harder.** One command installs it, creates the vault, and
wires your agent. No API key, no cloud account, no daemon. Unlock when you
want to use it; lock when you want it closed. By default an unlock stays
open for weeks (until restart or you lock it), like any app you leave
running. The security is free at the point of use because it falls out of
the architecture, not out of your patience: keeping plaintext off disk
forces the index into RAM, and a RAM-resident index is also the fastest
one there is. Secure and fast are the same choice here, and neither costs
you a configuration step.

## Install

One command per platform. Each installs the package, creates your
encrypted vault, and wires the agent.

**Claude (Code + Desktop)** - macOS / Linux:
```bash
pip install engram-memory-vault && engram init && engram integrate claude
```
Windows (PowerShell):
```powershell
py -m pip install engram-memory-vault; engram init; engram integrate claude
```
Registers the MCP server with the Claude Code CLI (user scope, all
projects), prints the Claude Desktop config block, and prints the one-line
CLAUDE.md instruction that makes Claude treat engRAM as its memory.

**Hermes** - macOS / Linux:
```bash
pip install engram-memory-vault && engram init && engram integrate hermes
```
Windows (PowerShell):
```powershell
py -m pip install engram-memory-vault; engram init; engram integrate hermes
```
Installs the provider plugin, wires the Hermes venv, and runs
`hermes memory setup engram`. engRAM then appears in the
`hermes memory setup` picker beside hindsight and mem0, the only entry
marked **"no setup needed"**: no API key, no cloud account, no daemon.
Verify with `hermes memory status`. See everything Hermes remembers at
any time with **`engram dash`** - one command, and the vault opens in
your browser (memories by kind, growth, the relation graph, live
search); Ctrl-C closes it.

**OpenClaw** - macOS / Linux:
```bash
pip install engram-memory-vault && engram init && engram integrate openclaw
```
Windows (PowerShell):
```powershell
py -m pip install engram-memory-vault; engram init; engram integrate openclaw
```
Writes the `mcpServers` entry into `~/.openclaw/openclaw.json` (with a
backup), then: `openclaw gateway restart` and confirm with
`openclaw mcp list`.

Anything else that speaks MCP gets the same server with one config block;
see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md). Until the PyPI release,
replace `pip install engram-memory-vault` with a clone + `pip install .`;
everything else is identical.

## Measured, on an 8 GB baseline laptop

Every number below is reproducible on your machine with `engram selftest`
and `engram bench`.

| Metric | Measured |
|---|---|
| Fresh install → open vault, offline | seconds, zero network |
| Vector search, 20k records (HNSW) | p95 0.68 ms |
| Full hybrid search (embed + vector + BM25 + fuse) | p95 8.8 ms |
| Peak RSS, model + vault + index resident | 319 MB |
| Store one memory (embed + encrypt + fsync journal) | ~40 ms |
| Wheel size, model included | ~30 MB |
| Test suite (crypto, tamper, crash, offline, concurrency, 2FA, graph, dash) | 88 tests, ~40 s |

A single network round-trip to a cloud memory API costs more than this
entire pipeline. The property that makes engRAM secure (no plaintext
index ever on disk, so all search is RAM-resident) is the same property
that makes it fast: below 20k records search is exact SIMD matrix math,
recall = 1.0 by construction; above it, SIMD HNSW at ~99% recall.

## The memory logic

Full write-path, decision math, and comparisons in
[docs/MEMORY.md](docs/MEMORY.md). The load-bearing ideas:

**Nearly everything is stored; nothing important is buried.** Only empty
turns are dropped. A bare "OK" is not noise, it is a decision: when the
agent asks *"Want me to send this reply to the client now?"* and the user
answers *"OK"*, engRAM resolves the question from the conversation and
stores
`[decision 2026-07-20] Approved (answered "OK"): Want me to send this
reply to the client now?` at the top importance tier. Asking *"did the
user say to email the client?"* later retrieves exactly that record.

**Deterministic importance tiers rank recall**: decisions/consent 0.90,
personal facts and preferences 0.80, the user's machine and configuration
0.75, other substantive statements 0.55, pleasantries 0.20 (kept, ranked
last). The fused score is
`RRF(vector) + RRF(keyword) + 0.02·cosine + 0.006·importance`: cosine
magnitude keeps the genuinely best match on top, importance settles
near-ties in favor of what matters. The agent learns the user and the
computer first, the world second, and forgets nothing.

**One pinned embedding space.** The model's SHA-256 is recorded in the
vault and enforced at open; cosine comparisons stay mathematically valid
forever instead of silently degrading when a model changes. Migration is
explicit: `engram reindex --re-embed`.

**No LLM inside.** Embeddings run locally (bundled 384-dim int8 ONNX
model, <300 MB RAM). Judgment belongs to the host model you already run,
via `engram_store` / `engram_forget`; engRAM contributes deterministic
capture, encryption, and total recall. That split is what makes the
offline guarantee absolute and every decision reproducible. Pair engRAM
with an offline LLM and the whole agent stack can run usefully with no
network at all.

## Agent-native by design

engRAM is built to sit under agents you already use, not as a separate
app you babysit.

- **Hermes native provider** - shows up in `hermes memory setup` with
  **"no setup needed"**. Turns sync automatically; search injects only
  what is relevant, tagged as data not instructions.
- **Claude over MCP** - one `integrate claude` step registers the server
  and gives you the Desktop config block plus a CLAUDE.md line so memory
  is part of normal work.
- **OpenClaw and any MCP client** - same stdio server, zero open ports,
  same tools (`memory_search`, `memory_store`, `memory_forget`, lock).
- **A memory graph, not just a memory pile** - `memory_link` records
  explicit relations (who works where, what belongs to what), with
  optional validity windows; `memory_relations` answers entity,
  predicate, and as-of queries. Deterministic storage, host-model
  judgment - the same split as everything else in engRAM.
- **CLI for everything else** - scripts, cron, other agents: `engram
  store`, `engram search`, `engram forget`, `engram link`,
  `engram relations`, `engram lock`.
- **See the vault: `engram dash`** - one command opens a local page with
  everything at a glance: how many memories of what kind, growth over
  time, the relation graph, tags, per-agent counts, live search. Served
  from RAM, 127.0.0.1-only behind a random URL token, read-only, zero
  outbound requests, zero configuration.
- **Panic lock from the agent** - `memory_lock` / `engram lock` clears
  stored credentials instantly when you need the vault closed now.
- **One vault, many hosts** - Hermes, Claude, and the CLI can share a
  vault at once; each caller gets its own identity and namespace ACLs.
- **One memory, no sections** - the starting memories seeded at `init`
  live in `main` as ordinary records, editable and forgettable like
  anything the agent stores; older vaults reorganize automatically.

Day to day, the point is simple: the agent remembers *you*, your
decisions, and your machine - encrypted, offline, and fast - without a
cloud account.

## The lock model

You lock and unlock the vault yourself whenever you want. Manual control
is always available:

- **`engram unlock`** - open the vault with YOUR passphrase. You choose
  it; engRAM never auto-generates a password, seed, or recovery phrase,
  and there is no credential it knows that you don't. (Vaults made by
  older versions that received an auto-generated recovery phrase still
  open with it.)
- **`engram lock`** - close it again and clear every stored credential.
  Agents can do the same via the `memory_lock` panic tool.
- **`engram 2fa enable`** - optional two-factor unlock: your passphrase
  (knowledge) plus a keyfile (possession - keep it on a USB stick).
  Both factors feed Argon2id together, so needing both is enforced by
  arithmetic, not a policy check; a stolen vault file plus your
  passphrase still opens nothing without the keyfile. One command, zero
  configuration: the keyfile's location is remembered, so day-to-day
  unlocking feels exactly the same while the file is present.

The default unlock mode is convenience, not a cage: after a normal
unlock, the vault stays usable across processes, logouts, and logins -
for weeks or months if you leave it that way - until the next restart or
power loss, or until you lock it yourself. Restart/power loss always
locks it: the stored credential is the master key wrapped by a key
derived from the kernel's boot timestamp plus the stable machine id; a
new boot can never open the old wrap. That is arithmetic, not a policy
check.

If you prefer reboot-surviving unlock on macOS, that is an explicit
opt-in (`engram unlock --keychain`), with the tradeoff documented. At any
time you can lock, unlock, lock again - on your schedule.

## Security, in one paragraph

XChaCha20-Poly1305 AEAD on everything at rest including vectors
(embedding-inversion resistance) · Argon2id keyslots, LUKS-style, opened
only by the user's own passphrase (no auto-generated credentials),
optionally two-factor with a keyfile · per-record keys enabling `forget --shred`
(crypto-shred: key destroyed, content mathematically unrecoverable) ·
fsync'd sealed journal, atomic compaction, verified kill-9 crash recovery ·
hash-chained tamper-evident audit log (`engram audit verify`) · per-caller
namespace ACLs, quarantine tier for untrusted content, signed vault
manifests · stdio MCP transport: zero open ports · runtime offline guard
that aborts on any socket attempt; CI runs the whole suite with it active
on Linux, macOS, and Windows · no telemetry, ever. Full honest threat
model, including what engRAM cannot protect against, in
[SECURITY.md](SECURITY.md).

## One vault, many agents

Hermes, Claude, and the CLI can share a single vault simultaneously:
writes are serialized by an advisory file lock, every process detects
foreign writes and reloads, and each host gets its own caller identity
and namespace with rw/ro grants. A locked vault is one portable file,
safe to move over any channel; `engram lock --sign` seals it with an
Ed25519 manifest the recipient can verify without any credential.

```bash
engram lock
scp ~/.engram/memory.vault other-machine:
engram --vault memory.vault unlock     # your passphrase (+ keyfile if 2FA)
```

## Documentation

| | |
|---|---|
| [docs/MEMORY.md](docs/MEMORY.md) | how memory is stored, what gets remembered, why the math wins |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | selecting engRAM in Hermes, OpenClaw, Claude, everything else |
| [SECURITY.md](SECURITY.md) | full threat model, honest limits |
| [FORMAT.md](FORMAT.md) | byte-level `.vault` and `.mpack` specs (language-agnostic) |
| [PACKS.md](PACKS.md) | authoring and shipping signed memory packs |

## License

MIT.
