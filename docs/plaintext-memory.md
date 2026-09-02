---
layout: default
permalink: /plaintext-memory/
title: "Your agent's memory is a plaintext file, and it has your API keys in it"
description: "Where the memory of Claude Code, Hermes, OpenClaw, the official MCP memory server and mem0 actually lives, what is in it, and what a memory store should do about that."
---

# Your agent's memory is a plaintext file, and it has your API keys in it

*Draft, September 2026. Every claim below links to the source it was checked against.*

Give a coding agent a memory tool and it will use it for everything. That is
the point. It is also the problem, because "everything" includes the database
URL it read out of `.env`, the token it fetched from your secrets manager to
finish a task, and the client's name and phone number from the ticket it was
working on. One Hacker News commenter watched it happen in real time:

> "You can witness first-hand how it stores credentials it fetches via the API
> of a secrets manager for stuff in plaintext too, despite being prompted not to
> do that."
> — [hammyhavoc, July 2025](https://news.ycombinator.com/item?id=44626811)

So it is worth asking where that memory actually lives.

## Where it lives

**Claude Code** keeps its memory as Markdown: `CLAUDE.md` files in the repo and
a `memory/` directory under `~/.claude` with an index that is loaded into every
session. Plain files, readable by any process running as you.

**Hermes Agent** keeps two files in `~/.hermes/memories/`: `MEMORY.md`, capped at
2,200 characters, and `USER.md`, capped at 1,375, both "injected into the system
prompt as a frozen snapshot at session start" ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)).
The caps are the interesting part: memory that has to fit in 800 tokens is
memory that has to be curated by hand.

**OpenClaw** keeps `MEMORY.md` for durable facts and a diary of
`memory/YYYY-MM-DD.md` logs ([docs](https://open-claw.bot/docs/concepts/memory/)).
Search over them needs an embedding provider, so recall is a network call.

**The official MCP memory server**, `@modelcontextprotocol/server-memory`,
which is the default memory for a large share of MCP users, writes a
`memory.jsonl` file. Its default location is the installed package directory
itself: `path.join(path.dirname(fileURLToPath(import.meta.url)), 'memory.jsonl')`
([source](https://github.com/modelcontextprotocol/servers/blob/main/src/memory/index.ts)).
Search is `toLowerCase().includes()` over names, types and observations. No
ranking, no expiry, no semantic match, no encryption.

**mem0**, the largest open-source memory layer, stores extracted facts in a
vector store after an LLM has rewritten them, so every store is a model call.
It also ships usage telemetry that is on unless you turn it off:
`MEM0_TELEMETRY = os.environ.get("MEM0_TELEMETRY", "True")`, posting to
PostHog with a project key that is in the source file
([telemetry.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/telemetry.py)).
A request to make it opt-in was
[closed as not planned](https://github.com/mem0ai/mem0/issues/2683). Its
MCP server, the thing an agent would actually talk to, is now hosted only:
"Nothing runs on your machine: the server is hosted by Mem0 ... Memories
you store this way live in your Mem0 account, not on your computer"
([docs](https://docs.mem0.ai/platform/mem0-mcp)).

**claude-mem**, the most-starred Claude Code memory plugin, keeps its store
locally but its installer "asks you to sign in to claude-mem in your browser
(email magic link)" and the hosted memory provider is a subscription after a
30-day trial ([README](https://github.com/thedotmack/claude-mem)).

None of the projects above document encryption at rest, and a source-level
look at about twenty-five open-source memory stores found the same: where
encryption exists at all, it protects a database password or an OAuth
token, never the memories. Most are excellent at
what they set out to do. But every one of them treats the memory the way we
treated browser cookies in 2005: a file, in the clear, wherever it landed.

## Why the memory is the most valuable file on the disk

A memory store is the union of everything the agent has been told, across
every project, forever. It is more sensitive than any single repo, because it
crosses them. It is more sensitive than your shell history, because the agent
writes down conclusions, not just commands. And unlike a `.env` file, nobody
audits it, because nobody reads it; that is what the agent is for.

The vectors are not a safe place to hide it either. Text can be substantially
reconstructed from its embedding
([Morris et al., 2023](https://arxiv.org/abs/2310.06816)), so a "vector
database beside a plaintext file" is two copies of the secret, not one.

## What a memory should do

The bar is not exotic. It is what a password manager has done for twenty years.

1. **Encrypt everything at rest**, vectors included, under a key derived from a
   passphrase only the user holds. Authenticated encryption, so tampering is
   detected rather than silently read back.
2. **Lock on reboot.** An unlock should live in volatile memory and die with
   the boot, so a copied disk and a copied credential file open nothing.
3. **Delete by destroying the key.** "Forget" should be a crypto-shred, not a
   flag in a row.
4. **Never open a socket.** Recall on every turn cannot depend on a network
   round trip, and a memory that phones home is not yours. This should be
   enforced at runtime and proven in CI, not promised in a README.
5. **Keep an audit trail** that cannot be edited without breaking a hash chain,
   because a memory that can be rewritten silently is a memory that can be
   poisoned silently.
6. **Stay small.** A memory layer that runs its own LLM either phones home or
   costs you gigabytes of RAM. The embedding model can be 30 MB and run on a
   CPU in 25 ms.

## What I built

[Compartment](https://github.com/MaxFreedomPollard/Compartment) is my attempt
at that list. It is an MCP server plus a menu bar app: one vault file,
XChaCha20-Poly1305 on every record and every vector, Argon2id keyslots, an
optional keyfile as a second factor, per-record keys so `forget --shred` is
real, a hash-chained audit log, and a runtime guard that aborts the process
if anything tries to open a socket. CI runs the entire test suite with that
guard active on Linux, macOS and Windows. The embedding model ships inside
the package; a full hybrid search takes about 12 ms from RAM. Claude Code,
Claude Desktop, Cursor, Hermes, OpenClaw and any other MCP client share the
same vault, each under its own namespace ACL.

What it does not do, so you do not have to find out: while the vault is
unlocked, any process running as your user can ask it questions, exactly as
with a password manager that is open. The model can still be talked into
recalling something it should not; recalled memories are wrapped as data, not
instructions, but that is a mitigation, not a guarantee. It is Python. And it
seeds the vault with a few thousand reference facts about hardware, ports and
encodings, which some people like and some people switch off.

If a Markdown file is working for you, keep it. This is for the day it has
your API key in it.
