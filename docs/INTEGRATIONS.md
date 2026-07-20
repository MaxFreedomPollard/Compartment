# Selecting engRAM as your agent's memory

How to make engRAM the active memory in each ecosystem. Three mechanisms
exist in the wild - a native provider slot (Hermes, OpenClaw), MCP tool
registration (Claude and most modern agents), and plain CLI/JSON (anything
that can run a subprocess). engRAM supports all three from one install.

---

## Claude (Code and Desktop) - MCP registration IS the selector

Claude has no memory-provider picker; registering an MCP server is how you
give Claude a memory. One command does everything:

```bash
pip install engram-vault && engram init && engram integrate claude
```

Or manually (Claude Code):

```bash
claude mcp add --scope user engram -- \
    engram --vault ~/.engram/memory.vault --caller claude-code serve
```

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "engram": {
    "command": "engram",
    "args": ["--vault", "/Users/you/.engram/memory.vault",
             "--caller", "claude-desktop", "serve"] } } }
```

The `memory_search` / `memory_store` / `memory_forget` / `memory_status` /
`memory_lock` tools then appear in every session, plus the memory-graph
tools `memory_link` / `memory_relations` / `memory_unlink` for relational
mapping (who works where, what belongs to what - with optional validity
windows and as-of queries). To make Claude treat it
as *the* memory (the analog of selecting a provider), add one standing
instruction to your `CLAUDE.md` / project memory:

> Use the engram `memory_search` tool to recall prior facts before
> answering questions about past work, and store durable facts and user
> decisions with `memory_store`.

## Hermes - native provider picker (works today)

One command:

```bash
pip install engram-vault && engram init && engram integrate hermes
```

Hermes selects external memory through `hermes memory setup`, an arrow-key
picker. `integrate hermes` does all of the following automatically; the
manual steps, for reference:

```bash
# 1. engram into the Hermes environment
python -m pip install engram-vault
# 2. the provider plugin (from this repo)
cp -r integrations/hermes/engram ~/.hermes/plugins/engram
# 3. a vault (once) - stays unlocked until reboot
engram init
# 4. SELECT IT
hermes memory setup
```

The picker then shows:

```
  byterover     - API key / local
  hindsight     - API key / local
  holographic   - local
  honcho        - API key / local
  mem0          - API key / local
  openviking    - API key / local
  retaindb      - API key / local
  supermemory   - requires API key
▸ engram       - no setup needed        ← choose this
  Built-in only - MEMORY.md / USER.md
```

engRAM is the only entry that needs no API key and no cloud account.
Selecting it writes `memory.provider: engram` to config.yaml; confirm with
`hermes memory status` (→ `Provider: engram`, `available ✓`). From then on
Hermes auto-recalls relevant memories before every turn, persists every
turn encrypted, and exposes `engram_search` / `engram_store` /
`engram_forget` tools.

**Shipping in Hermes out of the box** (no copy step for anyone): that
requires an upstream hermes-agent PR bundling this plugin under
`plugins/memory/engram` with `pip_dependencies: ["engram-vault"]` -
which in turn requires the PyPI release. Sequence: publish `engram-vault`
→ PR → every Hermes user sees engRAM in the picker by default.

## OpenClaw - one command (MCP), plugin slot planned

```bash
pip install engram-vault && engram init && engram integrate openclaw
```

`integrate openclaw` writes the server entry under `mcpServers` in
`~/.openclaw/openclaw.json` (backing the file up first), then:

```bash
openclaw gateway restart
openclaw mcp list        # → engram tools listed
```

OpenClaw's native memory selector is a plugin slot
(`plugins.slots.memory`, switched by `openclaw plugins install …`, the
mechanism its LanceDB memory uses). A native `openclaw-memory-engram`
slot plugin (auto-recall via the `before_prompt_build` hook, bridging to
the local engram engine) is planned; the MCP path above works today.

## Everything else

| Agent kind | Mechanism | What to do |
|---|---|---|
| Any MCP-capable host (Cursor, Windsurf, custom SDK agents, …) | MCP stdio | the same one-block config as Claude, with its own `--caller` name |
| Python frameworks (LangChain/LlamaIndex-style) | vector-store adapter | import `engram.vault.Vault` directly, or use the CLI; a drop-in `VectorStore` adapter class is on the roadmap |
| Anything that can run a subprocess | CLI/JSON | `engram search "query" --json`, `engram store "text"` |

One vault serves all of them at once: writes are serialized across
processes, every host gets its own `--caller` identity and namespace, and
the per-caller ACLs in `<vault>.config.json` decide who reads what.
