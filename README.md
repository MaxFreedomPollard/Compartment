# Compartment

### Superior agentic memory, encrypted at rest.

Your AI agent forgets you the moment the session ends. Compartment ends
that. With Compartment, your AI agent gets better with experience: it keeps
every decision, preference and detail you give it, permanently, encrypted,
on your own computer. Hermes, Claude,  OpenClaw and other AI Agents can
install in one command. One fully-transferable memory store is shared
simultaneously by all agents on the computer. 100% offline: no network, no
API key, no cloud account, no telemetry. The embedding model ships inside
the package, and a full search returns in under 9 ms, beating the round-trip
a hosted memory charges you for. Every byte at rest is AEAD-encrypted, the
embedding vectors included, and only your passphrase opens it.

Unlike other agentic memory, we offer an option to start off with memory -
6,718 curated facts seeded at install: the physical constants and unit
conversions, 800+ hardware facts with real specs (Apple silicon, PCs, CPUs
and GPUs, phones, game consoles, Raspberry Pi, storage, displays,
connectors) to provide a map of computer geography, operating system
versions and release names, network ports and HTTP, file signatures,
character encodings, shell and Unix internals, git, regex, SQL and hashing,
ISO country, currency and time codes, and more. Allows an offline agent to
operate better without internet, and an online agent to operate faster and
more accurately.

**More secure, by construction.** Every byte at rest is
authenticated-encrypted, the embedding vectors included (most tools leave
those in the clear, and vectors can be inverted back toward text). Deletion
is cryptographic: destroy the record's key and it is gone, unrecoverable.
Tampering is detected, history is hash-chained, and the vault locks itself
on restart or power loss. It runs fully offline: a runtime guard aborts on
any network attempt, and CI proves it on three operating systems.

**Not one step harder.** One command installs it, creates the vault, and
wires your agent. No API key, no cloud account, no daemon. Unlock when you
want to use it; lock when you want it closed. By default an unlock stays
open for weeks (until restart or you lock it), like any app you leave
running. The security is free at the point of use because it falls out of
the architecture, not out of your patience: keeping plaintext off disk
forces the index into RAM, and a RAM-resident index is also the fastest one
there is. Secure and fast are the same choice here, and neither costs you a
configuration step.

#### What sets it apart

**Install it in one step**

- One command installs it, creates the vault, and wires your agent. No API
  key, no cloud account, no daemon.
- On the Mac, open one `.pkg` and you are done. Python, the embedding model
  and every dependency are inside it.
- A menu bar app runs the whole thing without a terminal: vault state,
  unlock, lock, and the last five memories it saved.
- Every feature toggles in that panel instead of a config file:
  model-independent capture, starter facts in search, auto-lock.
- Your vault ships full. The 6,718 seeded facts are ordinary memories,
  editable and forgettable, and one switch keeps them out of search.
- Runs under what you already use: Hermes ("no setup needed"), Claude Code
  and Desktop over MCP, OpenClaw, every MCP client, plus a CLI for scripts
  and cron.

**Remembers the right things**

- "OK" is a decision, and Compartment files it as one, with the question it
  answered. That is the record you need later.
- Decisions beat preferences, preferences beat machine details, machine
  details beat chatter. A fixed ranking, not a model's mood.
- It forgets nothing. Small talk is kept and ranked last.
- It replaces your host's built-in memory instead of fighting it: imports
  what Claude Code already wrote, then supersedes it.
- It captures even when the model does not cooperate. A hook writes the fact
  whether or not the model calls the tool.
- A graph, not a pile. Explicit relations with validity windows answer who
  worked where, and when.

**Search that beats a network call**

- 0.68 ms vector search. 8.8 ms for the full hybrid pipeline. A cloud memory
  spends longer than that saying hello.
- Exact below 20k records: recall = 1.0 by construction, not an
  approximation.
- Hybrid always: meaning and keywords, fused.
- One pinned embedding space, enforced every time the vault opens, so your
  comparisons stay valid forever.

**Encrypted, offline, and yours**

- Every byte at rest is AEAD-encrypted, embedding vectors included. Most
  tools leave vectors in the clear, and vectors invert back toward text.
- Only your passphrase opens it. Compartment generates no password, no seed,
  no recovery phrase, and holds no credential you do not.
- Add a keyfile and unlock takes two factors. Both feed Argon2id together,
  so it is arithmetic, not a policy check.
- `forget --shred` destroys the record's key. The content is mathematically
  unrecoverable, not marked deleted.
- Restart or power loss locks it, and the agent has a panic lock that clears
  every credential instantly.
- 100% offline. A runtime guard aborts on any network attempt, and CI proves
  it on Linux, macOS and Windows. Zero open ports. No telemetry, ever.
- No LLM inside. Embeddings run locally in under 300 MB, and judgment stays
  with the model you already pay for.
- Tamper-evident: hash-chained audit log, sealed journal, verified kill-9
  crash recovery.
- One portable file. Move a locked vault anywhere, and `lock --sign` seals
  it with an Ed25519 manifest anyone can verify without a credential.
- `compartment dash` puts the entire vault on a local page: 127.0.0.1 only,
  random token, read-only.

## The memory logic

Full write-path, decision math, and comparisons in
[docs/MEMORY.md](docs/MEMORY.md). The load-bearing ideas:

**Nearly everything is stored; nothing important is buried.** Only empty
turns are dropped. A bare "OK" is not noise, it is a decision: when the
agent asks *"Want me to send this reply to the client now?"* and the user
answers *"OK"*, Compartment resolves the question from the conversation and
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

**One memory, not two.** Agent hosts increasingly ship a memory of their
own - Claude Code keeps per-project Markdown files with an auto-loaded
index. Two memories means facts land in whichever one the model happened to
think of, and neither is complete. Compartment takes over on install: it imports
what the file memory already holds, and both the MCP handshake and the
managed CLAUDE.md block tell the model that compartment supersedes it - write
every new memory here, treat the files as a read-only archive. One vault,
encrypted, shared by every agent and project on the machine. Nothing is
deleted; the files stay exactly where they were.

**Capture that does not depend on the model.** Instructions are a request,
and a host that declares its own memory in its system prompt outranks
anything a tool says. So `compartment integrate claude` also installs a
`PostToolUse` hook: when Claude Code writes a memory file, the fact lands in
the vault whether or not the model ever thought about compartment. The hook is
additive and idempotent (your other hooks are untouched, settings.json is
backed up first), it exits successfully no matter what - a memory tool must
never break your editor - and it stays quiet when the vault is locked.
`compartment hook status | install | uninstall`, or `integrate claude --no-hooks`.

<p align="center">
  <img src="https://raw.githubusercontent.com/MaxFreedomPollard/Compartment/main/docs/images/menubar-panel.png" width="330"
       alt="The Compartment panel in the macOS menu bar.">
  <img src="https://raw.githubusercontent.com/MaxFreedomPollard/Compartment/main/docs/images/windows-tray-panel.png" width="330"
       alt="The Compartment panel in the Windows notification area.">
</p>

**A menu bar and system tray app.** Download **Compartment.pkg** from the
[latest release](https://github.com/MaxFreedomPollard/Compartment/releases/latest)
and open it - the installer asks whether you also want the menu bar utility,
and everything (Python included) is self-contained, so there is nothing to
install first. From a checkout, `pip install 'compartment[menubar]'`
then `compartment menubar` does the same thing; on Windows,
`pip install 'compartment[tray]'` then `compartment tray`. Either way it puts
Compartment in the status bar: click the icon and a
panel shows whether the vault is open, how much it has learned, the three
settings worth changing day to day (capture hook, whether starter facts join
searches, auto-lock), and the last five things it remembered. Unlock, lock and
change your passphrase there too, without opening a terminal. No dock icon,
no window to manage, and it holds no vault in memory - state comes from the
CLI, so an idle app costs nothing.

**See what it just learned.** `compartment recent` lists the newest memories,
newest last, hiding the thousands of seeded starting facts so the handful
that real use produced are actually visible - and `compartment status` reports
`organic_records` beside the total, so a vault that has learned nothing can
never look busy. Same view over MCP as `memory_recent`.

**One pinned embedding space.** The model's SHA-256 is recorded in the
vault and enforced at open; cosine comparisons stay mathematically valid
forever instead of silently degrading when a model changes. Migration is
explicit: `compartment reindex --re-embed`.

**No LLM inside.** Embeddings run locally (bundled 384-dim int8 ONNX
model, <300 MB RAM). Judgment belongs to the host model you already run,
via `memory_store` / `memory_forget`; Compartment contributes deterministic
capture, encryption, and total recall. That split is what makes the
offline guarantee absolute and every decision reproducible. Pair Compartment
with an offline LLM and the whole agent stack can run usefully with no
network at all.

## Install

One command per platform. Each installs the package, creates your
encrypted vault, and wires the agent.

**Claude (Code + Desktop)** - macOS / Linux:
```bash
pip install compartment && compartment init && compartment integrate claude
```
Windows (PowerShell):
```powershell
py -m pip install compartment; compartment init; compartment integrate claude
```
Registers the MCP server with the Claude Code CLI (user scope, all
projects), **imports any memories Claude Code already wrote to its own
file-based memory** (copy-only - the Markdown files are never modified;
`--no-import` opts out, `compartment import-claude` does it later), and prints
the Claude Desktop config block. The server describes
itself over the MCP handshake - it tells the model to recall before answering
and to store durable facts, credentials, names, and decisions - so Claude
treats Compartment as its memory with no hand-written instruction; `integrate
claude` also writes a managed, idempotent block into your CLAUDE.md as backup.

**Hermes** - macOS / Linux:
```bash
pip install compartment && compartment init && compartment integrate hermes
```
Windows (PowerShell):
```powershell
py -m pip install compartment; compartment init; compartment integrate hermes
```
Installs the provider plugin, wires the Hermes venv, and runs
`hermes memory setup compartment`. Compartment then appears in the
`hermes memory setup` picker beside hindsight and mem0, the only entry
marked **"no setup needed"**: no API key, no cloud account, no daemon.
Verify with `hermes memory status`. See everything Hermes remembers at
any time with **`compartment dash`** - one command, and the vault opens in
your browser (memories by kind, growth, the relation graph, live
search); Ctrl-C closes it.

**OpenClaw** - macOS / Linux:
```bash
pip install compartment && compartment init && compartment integrate openclaw
```
Windows (PowerShell):
```powershell
py -m pip install compartment; compartment init; compartment integrate openclaw
```
Writes the `mcpServers` entry into `~/.openclaw/openclaw.json` (with a
backup), then: `openclaw gateway restart` and confirm with
`openclaw mcp list`.

**Any MCP client** - macOS / Linux / Windows:

```bash
pip install compartment && compartment init
```

Then add the server to your client's MCP config (stdio transport, no API
key, no environment variables):

```json
{
  "mcpServers": {
    "compartment": {
      "command": "compartment",
      "args": ["serve"]
    }
  }
}
```

`--vault` and `--caller` are optional (`compartment --vault PATH --caller NAME
serve`); the defaults use `~/.compartment/memory.vault` with caller `user`.
Client-by-client walkthroughs in
[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

## Measured, on an 8 GB baseline laptop

Every number below is reproducible on your machine with `compartment selftest`
and `compartment bench`.

| Metric | Measured |
|---|---|
| Fresh install → open vault, offline | seconds, zero network |
| Vector search, 20k records (HNSW) | p95 0.68 ms |
| Full hybrid search (embed + vector + BM25 + fuse) | p95 8.8 ms |
| Peak RSS, model + vault + index resident | 319 MB |
| Store one memory (embed + encrypt + fsync journal) | ~40 ms |
| Wheel size, model included | ~30 MB |
| Test suite (crypto, tamper, crash, offline, concurrency, 2FA, graph, dash) | 199 tests, ~60 s |

A single network round-trip to a cloud memory API costs more than this
entire pipeline. The property that makes Compartment secure (no plaintext
index ever on disk, so all search is RAM-resident) is the same property
that makes it fast: below 20k records search is exact SIMD matrix math,
recall = 1.0 by construction; above it, SIMD HNSW at ~99% recall.

## Agent-native by design

Compartment is built to sit under agents you already use, not as a separate
app you babysit.

- **Hermes native provider** - shows up in `hermes memory setup` with
  **"no setup needed"**. Turns sync automatically; search injects only
  what is relevant, tagged as data not instructions.
- **Claude over MCP** - one `integrate claude` step registers the server
  and gives you the Desktop config block plus a managed CLAUDE.md block so memory
  is part of normal work.
- **OpenClaw and any MCP client** - same stdio server, zero open ports,
  same tools (`memory_search`, `memory_store`, `memory_forget`, lock).
- **A memory graph, not just a memory pile** - `memory_link` records
  explicit relations (who works where, what belongs to what), with
  optional validity windows; `memory_relations` answers entity,
  predicate, and as-of queries. Deterministic storage, host-model
  judgment - the same split as everything else in Compartment.
- **CLI for everything else** - scripts, cron, other agents: `compartment
  store`, `compartment search`, `compartment recent`, `compartment forget`, `compartment link`,
  `compartment relations`, `compartment import-claude`, `compartment lock`.
- **See the vault: `compartment dash`** - one command opens a local page with
  everything at a glance: how many memories of what kind, growth over
  time, the relation graph, tags, per-agent counts, live search. Served
  from RAM, 127.0.0.1-only behind a random URL token, read-only, zero
  outbound requests, zero configuration.
- **Panic lock from the agent** - `memory_lock` / `compartment lock` clears
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

- **`compartment unlock`** - open the vault with YOUR passphrase. You choose
  it; Compartment never auto-generates a password, seed, or recovery phrase,
  and there is no credential it knows that you don't. (Vaults made by
  older versions that received an auto-generated recovery phrase still
  open with it.)
- **`compartment lock`** - close it again and clear every stored credential.
  Agents can do the same via the `memory_lock` panic tool.
- **`compartment 2fa enable`** - optional two-factor unlock: your passphrase
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
opt-in (`compartment unlock --keychain`), with the tradeoff documented. At any
time you can lock, unlock, lock again - on your schedule.

## Security, in one paragraph

XChaCha20-Poly1305 AEAD on everything at rest including vectors
(embedding-inversion resistance) · Argon2id keyslots, LUKS-style, opened
only by the user's own passphrase (no auto-generated credentials),
optionally two-factor with a keyfile · per-record keys enabling `forget --shred`
(crypto-shred: key destroyed, content mathematically unrecoverable) ·
fsync'd sealed journal, atomic compaction, verified kill-9 crash recovery ·
hash-chained tamper-evident audit log (`compartment audit verify`) · per-caller
namespace ACLs, quarantine tier for untrusted content, signed vault
manifests · stdio MCP transport: zero open ports · runtime offline guard
that aborts on any socket attempt; CI runs the whole suite with it active
on Linux, macOS, and Windows · no telemetry, ever. Full honest threat
model, including what Compartment cannot protect against, in
[SECURITY.md](SECURITY.md).

## One vault, many agents

Hermes, Claude, and the CLI can share a single vault simultaneously:
writes are serialized by an advisory file lock, every process detects
foreign writes and reloads, and each host gets its own caller identity
and namespace with rw/ro grants. A locked vault is one portable file,
safe to move over any channel; `compartment lock --sign` seals it with an
Ed25519 manifest the recipient can verify without any credential.

```bash
compartment lock
scp ~/.compartment/memory.vault other-machine:
compartment --vault memory.vault unlock     # your passphrase (+ keyfile if 2FA)
```

## Documentation

| | |
|---|---|
| [docs/MEMORY.md](docs/MEMORY.md) | how memory is stored, what gets remembered, why the math wins |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | selecting Compartment in Hermes, OpenClaw, Claude, everything else |
| [SECURITY.md](SECURITY.md) | full threat model, honest limits |
| [FORMAT.md](FORMAT.md) | byte-level `.vault` and `.mpack` specs (language-agnostic) |
| [PACKS.md](PACKS.md) | authoring and shipping signed memory packs |
| [RELEASING.md](RELEASING.md) | cutting a release: every download, every time |

---

mcp-name: io.github.MaxFreedomPollard/compartment

