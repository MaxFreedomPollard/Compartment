# Compartment

[![MCP Toplist](https://mcptoplist.com/badge/io.github.MaxFreedomPollard%2Fcompartment.svg)](https://mcptoplist.com/server/io.github.MaxFreedomPollard%2Fcompartment)

<a href="https://glama.ai/mcp/servers/MaxFreedomPollard/Compartment"><img width="380" height="200" src="https://glama.ai/mcp/servers/MaxFreedomPollard/Compartment/badge" alt="Compartment MCP server" /></a>

### Durable agentic memory, encrypted at rest.

Your AI agent forgets you the moment the session ends. Compartment ends
that. With Compartment, your AI agent gets better with experience: it keeps
every decision, preference and detail you give it, permanently, encrypted,
on your own computer. Hermes, Claude, OpenClaw and other AI Agents can
install in one command. One fully-transferable memory store is shared
simultaneously by all agents on the computer. 100% offline: no network, no
API key, no cloud account, no telemetry. The embedding model ships inside
the package, and a full search returns in about 12 ms, beating the round-trip
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

## Install

**One line install, works on all operating systems.**

```bash
pip install compartment && compartment init
```

Then connect it to the agent you use:

```bash
compartment integrate claude
```

`claude`, `hermes` and `openclaw` are the three auto-connect targets. Each one
also gets the **`/compartmentalize`** skill installed into its own skills
directory.

**`/compartmentalize` saves the conversation before it is thrown away.** Every
agent eventually compacts or summarizes a long session, and the summary is
written by a pass that has no tools, so nothing can be stored from inside it:
whatever the model did not think to save is simply gone. Type
`/compartmentalize` and the whole conversation is swept into the vault first -
people and contacts, credentials and where they live, URLs and hosts, decisions
and the reasoning behind them, and a narrative of the session itself. Then
compact, and nothing is lost. It works on its own at any point too.

**One click install (for people not good with command line).** Download
**Compartment.pkg** from the [latest release](https://github.com/MaxFreedomPollard/Compartment/releases/latest)
and open it. Python, the embedding model and every dependency are inside it.
macOS only.

After install, everything is managed from the app: the **menu bar** on macOS,
the **notification area** on Windows, and a **window** from your applications
menu on Linux.

Compartment is an MCP server, so it works with all MCP capable agentic AI out
of the box. Every option is in [Configuration](#configuration).

#### What sets it apart

**Install it in one step**

- One command installs it, creates the vault, and wires your agent. No API
  key, no cloud account, no daemon.
- On the Mac, open one `.pkg` and you are done. Python, the embedding model
  and every dependency are inside it.
- An app runs the whole thing without a terminal on all three systems: vault
  state, unlock, lock, and the last five memories it saved. The macOS menu
  bar, the Windows notification area, a window on Linux.
- Every feature toggles in that panel instead of a config file:
  model-independent capture, starter facts in search, auto-lock.
- `/compartmentalize` is installed into every agent it connects, so one command
  banks a whole conversation before compaction throws it away.
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

- 0.68 ms vector search. About 12 ms for the full hybrid pipeline. A cloud memory
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
last). Importance multiplies a match rather than adding to it, so it settles
near-ties in favour of what matters and can never surface a memory for a
question it has nothing to do with. The whole scoring model, and the numbers
it was chosen against, are in [The mathematics](#the-mathematics). The agent
learns the user and the computer first, the world second, and forgets
nothing.

**One memory, not two.** Agent hosts increasingly ship a memory of their
own - Claude Code keeps per-project Markdown files with an auto-loaded
index. Two memories means facts land in whichever one the model happened to
think of, and neither is complete. Compartment takes over on install: it imports
what the file memory already holds, and both the MCP handshake and the
managed CLAUDE.md block tell the model that Compartment supersedes it - write
every new memory here, treat the files as a read-only archive. One vault,
encrypted, shared by every agent and project on the machine. Nothing is
deleted; the files stay exactly where they were.

**Capture that does not depend on the model.** Instructions are a request,
and a host that declares its own memory in its system prompt outranks
anything a tool says. So `compartment integrate claude` also installs a
`PostToolUse` hook: when Claude Code writes a memory file, the fact lands in
the vault whether or not the model ever thought about Compartment. The hook is
additive and idempotent (your other hooks are untouched, settings.json is
backed up first), it exits successfully no matter what - a memory tool must
never break your editor - and it stays quiet when the vault is locked.
`compartment hook status | install | uninstall`, or `integrate claude --no-hooks`.

**An app on all three systems, from the same one install.** `pip install
compartment && compartment init` sets it up on macOS, Windows and Linux.
There is no separate package, no extra to remember and no second command. On
the Mac you can instead open **Compartment.pkg** from the
[latest release](https://github.com/MaxFreedomPollard/Compartment/releases/latest),
which carries Python and every dependency inside it, so there is nothing to
install first. `compartment init --no-app` skips the app for headless boxes
and CI.

The same panel, in the place each system keeps things like this: the **menu
bar** on macOS, the **notification area** on Windows, and on **Linux** an
ordinary window, with Compartment in your applications menu. Linux gets a
window rather than an icon deliberately. Whether a tray icon appears there
depends on the desktop, and on GNOME or Wayland it can simply never show up
with nothing said, which is the worst way for the control that unlocks your
memories to fail.

The panel shows whether the vault is open, how much it has learned, the three
settings worth changing day to day (capture hook, whether starter facts join
searches, auto-lock), which agents are connected and buttons to connect them,
and the last five things it remembered. Unlock, lock and change your
passphrase there too, without opening a terminal. It holds no vault in
memory - state comes from the CLI, so an idle app costs nothing.

**See what it just learned.** `compartment recent` lists the newest memories,
newest last, hiding the thousands of seeded starting facts so the handful
that real use produced are actually visible - and `compartment status` reports
`organic_records` beside the total, so a vault that has learned nothing can
never look busy. Same view over MCP as `memory_recent`.

**One fact per memory, dated.** A memory is an atomized data point, not a
session log. `memory_store_many` takes a whole batch in one call, so storing
six facts separately costs the same one round trip as bundling them into a
longer format description - which is what made agents write longer format
descriptions. Every memory carries the moment it was saved, and separately the
day the fact was discovered together with how it was established, appended as a
short `[web search, 2026-08-01]` clause. Those are two different dates: a price
you check on the Friday and write up on the Monday keeps Friday as its
discovery and Monday as its save.

**Search returns what is relevant, not a fixed number.** How many memories
answer a question is a property of the question, so Compartment returns every
memory whose evidence stands up against the best answer to that same question,
capped generously. The cut has to be relative, because scores are not
comparable between questions - on a real vault the nonsense query "how to bake
sourdough bread" scored higher than the genuine "what did Max decide about
Airtable". Ask it something the vault knows nothing about and it returns
nothing at all rather than a page of polite irrelevancies. Pass an explicit
`top_k` when you want exactly that many.

**Tags that stay true.** What a memory is about never changes. What it is
relevant to changes constantly, and a tag written once, on the day the memory
was stored, cannot know that.

Here is an example of the failure this solves. Working on a project called
Northwind, you learn that your client wants figures before conclusions: never
open with the recommendation, open with the numbers. That is a durable fact
about a person. The agent stores it and tags it `northwind`, `reporting`,
because Northwind is what was in front of it that day.

Northwind ends. Two years later the same client, now going by the name Harbour,
hires you again. Your agent narrows recall to `harbour`, the way anyone narrows
a search once a vault holds thousands of memories. The one thing you most want
applied is filed under a name that no longer exists. It is still true and still
exactly the right rule, but a tag-filtered search cannot return it, because tag
filtering is a subset match and a memory lacking the tag is simply not in the
set. The memory did not decay. Its index entry did.

Compartment repairs that automatically, offline, in the background, without
using an LLM. As Harbour memories accumulate - the client asking for numbers up
front again, a deck reordered to lead with them - they land by design beside
that old preference in embedding space, because they are about the same subject
and placed similarly through ordered logic. A background pass gives every
memory the tags its nearest neighbours carry, weighted by cosine, and the
preference picks up `harbour` from them. Two more offline signals run alongside
it: tags that nearly always occur together come to imply one another, and any
existing tag whose phrase appears in a memory's own text is attached. Nothing
in Compartment ever knew what Northwind or Harbour were.

The pass can only write the tags column, never the text, the dates or the
embeddings. It is additive unless you pass `--prune`, `tags_origin` keeps the
tags a memory was born with forever, and `compartment retag --dry-run` shows
exactly what would change before anything does.

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

## The mathematics

Everything below lives in one file, [`src/compartment/ranking.py`](src/compartment/ranking.py),
which the vault, the dashboard and the benchmark all import. A benchmark score
is therefore a measurement of the product and not of a copy of it that has
drifted.

### Storage: a memory is embedded in windows, not truncated

The encoder reads 512 tokens. Text past that is not weighted less, it is not
seen at all, so a long memory used to be searchable only by its opening. On a
real 6,705-memory vault, 40% of records ran past the window and **57.6% of the
whole corpus was invisible to semantic search**.

So a record is embedded as overlapping windows of `W = 448` tokens at a stride
of `S = 384`, giving 64 tokens of overlap so no fact is cut in half by a
boundary, and the record is scored by its best window:

```
windows(d) = ceil( max(0, tokens(d) - W) / S ) + 1        capped at 64

s_vec(d)   = max over windows w of d :  cos(q, w)
```

Max-pooling, not averaging: a memory is relevant if **any** part of it is, and
an average would punish a long memory for the parts that are about something
else. With one window per record it reduces exactly to the old behaviour, so it
can never be worse for a short memory. The cost is small because most memories
are short: on that vault, 6,705 records produced 6,785 windows.

Windows are measured in model tokens, never characters. A character budget is
wrong by a factor of three between prose and a hex digest, and being wrong here
means silently dropping the end of a memory.

### Recall: two channels, combined as evidence rather than added

Two indexes look for a memory and they answer different questions. The vector
index answers *what does this mean*. The keyword index answers *what does this
say*. Their scores are not denominated in the same thing, and combining them is
the entire difficulty.

The obvious move, and what Compartment shipped until now, is to add them.
Adding is the wrong operation: it lets a merely-good semantic match outvote
conclusive literal evidence. Searching a real vault for a commit sha occurring
in exactly one memory out of 6,705 returned that memory **below ten paraphrases
of it** - the keyword index had ranked it first and the sum buried it.

The two channels are not addends, they are **alternatives**: either one alone
can establish relevance. That is a soft OR over independent evidence,

```
P(relevant) = 1 - (1 - p_vec)(1 - p_lex)
```

and the score is its logarithm, which ranks identically while continuing to
spread results apart near the top instead of saturating at 1:

```
score(d) = - w_vec · log(1 - p_vec(d))  -  w_lex · log(1 - p_lex(d))

w_vec = 0.75      w_lex = 0.25
```

Either channel approaching certainty carries the memory on its own, and neither
can veto the other.

**Reading a cosine as a probability.** An L2-normalized encoder gives cosines
that are comparable *across* queries, so they map through fixed bounds.
Per-query min-max normalization is the obvious alternative and it is a trap: it
rescales the best hit of a hopeless query up to 1.0 and throws that calibration
away.

```
p_vec(d) = clamp( (cos(q, d) - 0.25) / (0.85 - 0.25),  0,  0.88 )
```

That ceiling of 0.88 is doing real work. A cosine is a similarity, never an
identity: an encoder can say *this is about the same thing*, but it can never
say *this is the record you named*. A literal match on a string unique to one
memory can say exactly that. So the semantic channel is capped below the
certainty the literal channel may reach, and the bound is forced rather than
chosen - the literal channel tops out at `0.25 · -log(1 - 0.999) = 1.727`, so
the cap must satisfy `0.75 · -log(1 - cap) < 1.727`, giving `cap < 0.90`.

**Reading a keyword hit as a probability, and deliberately not with BM25.**
BM25 answers *how well does this match*, which is not what settles a contest
against a semantic hit. What settles it is how unlikely the match was by
chance. So each query term carries its self-information over the vault, and a
memory scores the **fraction of the query's information it accounts for**:

```
I(t)     = log( N / (1 + df(t)) )                     N = records in the vault

p_lex(d) = ( Σ I(t) for query terms t present in d ) / ( Σ I(t) for all t )
```

A term unique to one memory is near-conclusive evidence. A term appearing in a
tenth of the vault is nearly none, whatever its BM25 happens to be. This is the
piece that makes a literal hit and a semantic hit comparable at all.

The keyword index is queried as AND first, since an exact phrase match is the
strongest signal available. FTS5's implicit AND means a nine-word question has
to appear word for word, so when AND finds nothing it falls back to OR over
only the terms carrying information - anything appearing in more than 10% of
records is dropped. That ceiling is measured from the vault rather than taken
from an English stopword list, so it behaves the same for a vault full of code,
of names, or of another language.

A small rank-agreement residue is added, the one thing reciprocal-rank fusion is
genuinely good at, sized to break ties rather than decide them:

```
+ w_rrf · k · [ 1/(k + rank_vec) + 1/(k + rank_lex) ]      w_rrf = 0.10, k = 20
```

### Importance ranking: priors multiply, they never add

```
final(d) = score(d) · ( 1 + w_imp · (2·importance(d) - 1)
                          + w_rec · 2^( -age_days(d) / 180 ) )

w_imp = 0.15      w_rec = 0.10
```

**Multiplicative, so a prior can only reorder a memory that already matched.**
An additive prior lets a very important memory surface for a question it has
nothing to do with, which is how a memory system starts feeling haunted. A
memory that matched nothing scores zero, and nothing can lift it off zero.

**Centred on the 0.5 default**, which is why `2·importance - 1` appears rather
than `importance`. Every unweighted memory carries 0.5, including the thousands
of starting facts a vault ships with. Uncentred, they all collect the same
silent boost, which is another way of saying importance did nothing at all.
Centred, an unweighted memory is exactly neutral and a deliberate weight is the
only thing that moves.

The tiers the capture path writes: decisions and consent 0.90, personal facts
and preferences 0.80, the user's machine and configuration 0.75, other
substantive statements 0.55, pleasantries 0.20. Recency halves every 180 days.

### Retrieval order, and why the pool is wide

Namespace, tag, date and starter-fact filters run *after* ranking, so a
candidate pool sized to the number of results requested can be emptied by them
while matching memories sit just past the cut. The pool starts at 200 per
channel and widens up to three times when filtering leaves too few.

### Measured

Against the previous scorer, end to end through `Vault.search`, on a real
6,705-memory vault with 44 queries in four families:

| | before | after |
|---|---|---|
| Recall@1 | 0.523 | **0.773** |
| Recall@5 | 0.705 | **0.977** |
| MRR@10 | 0.601 | **0.845** |
| nDCG@10 | 0.627 | **0.878** |
| exact identifiers found in top 5 | 4/10 | **10/10** |
| facts past the encoder window | 0/6 | **5/6** |
| paraphrases | 16/16 | 16/16 |
| median search latency | 4.4 ms | 11.6 ms |

Nothing regressed in any family. The weights were chosen from a sensitivity
sweep and are deliberately round: the result is flat around them, because a
ranker that only works at `w_lex = 0.37` is a ranker that does not work.

## Wiring each agent

One command per platform. Each installs the package, creates your
encrypted vault, and wires the agent.

Every one of these is also a button in the app. Click the Compartment icon
in your menu bar or notification area, and under **CONNECT AN AGENT** press
Claude, Hermes or OpenClaw. The button runs the same `compartment integrate`
command for you, so nobody has to open a terminal a second time.

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

`compartment integrate --list` names the thirty MCP clients it knows - Cursor,
VS Code, Cline, Roo Code, Zed, OpenCode, Codex CLI, Gemini CLI, LM Studio,
AnythingLLM, BoltAI and the rest - and `compartment integrate --all` writes the
block below into every one of them that is installed here.

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
| Full hybrid search (embed + windows + BM25 + evidence fusion) | median 11.6 ms, p95 14.7 ms |
| Peak RSS, model + vault + index resident | 319 MB |
| Store one memory (embed + encrypt + fsync journal) | ~40 ms |
| Wheel size, model included | ~30 MB |
| Test suite (crypto, tamper, crash, offline, concurrency, 2FA, graph, dash, ranking) | 566 tests, ~110 s |

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
locks it: the stored credential is the master key wrapped under a random
32-byte per-boot secret, held in a volatile kernel object that is never
written to any filesystem, so a restart destroys it and a new boot can
never open the old wrap. A copy of the credential file on its own is
useless, because the key it needs was never on the disk. That is
arithmetic, not a policy check.

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

## Configuration

Nothing here is required. Compartment installs configured, and this is the
whole surface if you want to change something.

### In the app

The panel behind the icon: **Unlock** and **Lock**, **Change password**,
**Create memories automatically** (the capture hook), **Search starter
facts**, **Auto-lock** (15, 30, 60 minutes or never), the **CONNECT AN
AGENT** buttons for Claude, Hermes and OpenClaw, **Refresh** and **Quit**.

`compartment panel --login on | off | status` controls starting at login,
which on Linux is the applications menu entry.

### Commands

| Command | What it does |
|---|---|
| `init` | create the vault. `--passphrase`, `--creator`, `--keychain`, `--no-session`, `--no-app` |
| `unlock` / `lock` | open or close it. `--passphrase-stdin`, `--keyfile`, `--keychain`, `--once`; `lock --sign --identity` |
| `status` / `verify` / `selftest` | what is in it, is it intact, does it work |
| `store` / `get` / `forget` | one memory. `--namespace`, `--tag`, `--importance`, `--quarantined`, `forget --shred` |
| `search` / `recent` | find things. `--namespace`, `--tag`, `--top-k`, `--limit`, `--all`, `--json` |
| `link` / `relations` / `unlink` | the relation graph, with validity windows (`--from`, `--to`, `--as-of`) |
| `panel` (`menubar`, `tray`) | the app. `--show`, `--self-check`, `--render`, `--login` |
| `integrate <agent>` | wire claude, hermes or openclaw, and install `/compartmentalize` for it. `--no-import`, `--no-hooks` |
| `hook` | capture hook: `install --pin-vault`, `uninstall`, `status`, `capture` |
| `serve` | the MCP server, over stdio |
| `dash` | read the vault in a browser: 127.0.0.1, one-time token, GET only |
| `export` / `import` | `export --plaintext` writes it unencrypted; `import` reads it back |
| `import-claude` | pull in what Claude Code already wrote. `--dir`, `--namespace`, `--dry-run` |
| `rekey` | change the passphrase. `--new-passphrase-stdin` |
| `2fa` | `enable`, `disable`, `status` - a keyfile as a second factor |
| `audit` | `verify`, `repair` the hash-chained history |
| `pack` | `build`, `install`, `remove`, `list`, `export` signed memory packs (`--trusted-key`) |
| `reindex` | rebuild the index, and give long records the embedding windows they are missing. `--int8`, `--f32`, `--re-embed`, `--model` |
| `bench` | `--records`, `--longmemeval`, `--variant`, `--limit` |
| `setup` | `download-model`, `download-longmemeval`, `airgap-bundle` |
| `update` | upgrade in place. `--source` takes GitHub main, `--no-app` skips the restart |
| `uninstall` | remove it. The vault is kept unless you pass `--purge` |

Global flags, before the command: `--vault PATH`, `--caller NAME`,
`--keyfile PATH`, `--assert-offline`, `--version`.

### The /compartmentalize skill

`compartment integrate <agent>` writes one file into that agent's own skills
directory, and `compartment uninstall` takes it back:

| Agent | Path |
|---|---|
| Claude Code | `~/.claude/skills/compartmentalize/SKILL.md` |
| Hermes | `$HERMES_HOME` or `~/.hermes/skills/compartmentalize/SKILL.md` |
| OpenClaw | `$OPENCLAW_HOME` or `~/.openclaw/skills/compartmentalize/SKILL.md` |

All three read the same Agent Skills layout, so it is one packaged file. It is
user-invoked only: no agent runs it on its own guess. Edit your copy freely -
a later install backs up anything that differs rather than overwriting it, and
leaves the backup behind when the skill is removed. Invoking it makes the agent
sweep the conversation and write to the vault, so expect a burst of
`memory_store` calls; that is the point of it.

### Settings file

`<vault>.config.json`, beside the vault, holding grants per caller and:

| Setting | Default | Meaning |
|---|---|---|
| `auto_lock_minutes` | `30` | idle time before it locks. `0` never locks |
| `search_starter_facts` | `true` | whether the seeded facts join search results |
| `include_packs_in_search` | `true` | the same, for installed packs |
| `duplicate_threshold` | `0.97` | cosine similarity at which a store is a duplicate |
| `index_precision` | `"f32"` | `"int8"` uses a quarter of the RAM |
| `retag_interval_hours` | `6` | how often the background pass re-derives tags. `0` turns it off |
| `retag_prune` | `false` | whether that pass may also REMOVE tags the vault no longer supports |
| `unlock_tool_enabled` | `false` | lets an agent unlock the vault. Off because the passphrase would cross the model's context |

### Environment

`COMPARTMENT_VAULT` which vault to use, `COMPARTMENT_PASSPHRASE` for scripts
and CI, `COMPARTMENT_SESSION_DIR` where the unlock credential lives,
`COMPARTMENT_UI_SCALE` panel scale, `COMPARTMENT_ASSERT_OFFLINE` abort on any
network attempt. `HERMES_HOME`, `OPENCLAW_HOME` and `XDG_DATA_HOME` are read
where they apply. Anything exported as `ENGRAM_*` still works.

### MCP tools

`memory_search`, `memory_store`, `memory_store_many`, `memory_get`,
`memory_recent`, `memory_forget`, `memory_link`, `memory_relations`, `memory_unlink`,
`memory_list_namespaces`, `memory_status`, `memory_lock`, `memory_selftest`.
`memory_unlock` exists but is off unless you turn it on above.

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

