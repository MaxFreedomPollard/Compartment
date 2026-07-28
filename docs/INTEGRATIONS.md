# Selecting Compartment as your agent's memory

How to make Compartment the active memory in each ecosystem. Three mechanisms
exist in the wild - a native provider slot (Hermes, OpenClaw), MCP tool
registration (Claude and most modern agents), and plain CLI/JSON (anything
that can run a subprocess). Compartment supports all three from one install.

---

## Claude (Code and Desktop) - MCP registration IS the selector

Claude has no memory-provider picker; registering an MCP server is how you
give Claude a memory. One command does everything:

```bash
pip install compartment && compartment init && compartment integrate claude
```

Or manually (Claude Code):

```bash
claude mcp add --scope user compartment -- \
    compartment --vault ~/.compartment/memory.vault --caller claude-code serve
```

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "compartment": {
    "command": "compartment",
    "args": ["--vault", "/Users/you/.compartment/memory.vault",
             "--caller", "claude-desktop", "serve"] } } }
```

### Claude Code already has a memory - Compartment takes it over

Claude Code keeps its own memory as Markdown files in a per-project
`memory/` directory with an auto-loaded `MEMORY.md` index. Left alone, you
end up with two memories: the model writes to whichever it thinks of first,
and neither is complete. `compartment integrate claude` resolves it in three ways:

1. **It imports what is already there.** Every memory file is parsed
   (frontmatter `type` becomes the importance tier, the name becomes a tag)
   and stored in the vault. The source files are read, never modified, and
   re-running imports nothing twice - so it is safe on every re-run.
2. **It tells the model which one wins.** The MCP handshake instructions and
   the managed CLAUDE.md block both state that Compartment supersedes the file
   memory: write new memories with `memory_store`, treat the directory as a
   read-only archive. This matters because the host declares its own memory
   in the system prompt, which otherwise outranks anything a tool says.

3. **It stops relying on the model.** `integrate claude` installs a
   `PostToolUse` hook into `~/.claude/settings.json`. When Claude Code writes
   a file into its memory directory, the hook stores it in the vault - no
   tool call required, so capture is deterministic instead of discretionary.
   Your existing hooks are preserved, settings.json is backed up first, and
   re-installing updates in place rather than duplicating. The hook always
   exits 0 (a memory tool must never break an edit) and does nothing when the
   vault is locked - `compartment import-claude` sweeps up anything missed.

```bash
compartment import-claude --dry-run     # see exactly what would be imported
compartment import-claude               # do it (idempotent)
compartment integrate claude --no-import  # wire up, but skip the import
compartment integrate claude --no-hooks   # wire up, but install no hook

compartment hook status                 # is the capture hook installed?
compartment hook install                # add it later
compartment hook uninstall              # remove compartment's hook only
```

Nothing is deleted. If you want the Markdown files gone afterwards, remove
them yourself once `compartment recent` shows the imported memories.

### Seeing what memory learned

```bash
compartment recent            # newest memories, newest last
compartment recent --limit 50
compartment recent --all      # include the seeded starting facts
compartment status            # organic_records vs seeded_records
```

`compartment search` ranks by relevance, which cannot answer "what did you just
remember?" - `recent` is that view, and `memory_recent` is the same thing
over MCP.

The `memory_search` / `memory_store` / `memory_recent` / `memory_forget` /
`memory_status` / `memory_lock` tools then appear in every session, plus the memory-graph
tools `memory_link` / `memory_relations` / `memory_unlink` for relational
mapping (who works where, what belongs to what - with optional validity
windows and as-of queries).

You do **not** need to hand-write a behavioral instruction: the server
advertises its own operating manual over the MCP `initialize` handshake, so
every host renders it in its "MCP Server Instructions" section and knows to
recall before answering and to store durable facts, credentials, names,
addresses, and decisions - on any machine, with no config. `compartment integrate
claude` additionally writes a managed (sentinel-fenced, idempotent) block into
your `~/.claude/CLAUDE.md` as belt-and-suspenders for the Claude Code path.

## Hermes - native provider picker (works today)

One command:

```bash
pip install compartment && compartment init && compartment integrate hermes
```

Hermes selects external memory through `hermes memory setup`, an arrow-key
picker. `integrate hermes` does all of the following automatically; the
manual steps, for reference:

```bash
# 1. compartment into the Hermes environment
python -m pip install compartment
# 2. the provider plugin (from this repo)
cp -r integrations/hermes/compartment ~/.hermes/plugins/compartment
# 3. a vault (once) - stays unlocked until reboot
compartment init
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
▸ compartment       - no setup needed        ← choose this
  Built-in only - MEMORY.md / USER.md
```

Compartment is the only entry that needs no API key and no cloud account.
Selecting it writes `memory.provider: compartment` to config.yaml; confirm with
`hermes memory status` (→ `Provider: compartment`, `available ✓`). From then on
Hermes auto-recalls relevant memories before every turn, persists every
turn encrypted, and exposes `compartment_search` / `compartment_store` /
`compartment_forget` tools.

**Shipping in Hermes out of the box** (no copy step for anyone): that
requires an upstream hermes-agent PR bundling this plugin under
`plugins/memory/compartment` with `pip_dependencies: ["compartment>=2.3"]` -
which in turn requires the PyPI release. Sequence: publish `compartment`
→ PR → every Hermes user sees Compartment in the picker by default.

## OpenClaw - one command (MCP), plugin slot planned

```bash
pip install compartment && compartment init && compartment integrate openclaw
```

`integrate openclaw` writes the server entry under `mcpServers` in
`~/.openclaw/openclaw.json` (backing the file up first), then:

```bash
openclaw gateway restart
openclaw mcp list        # → compartment tools listed
```

OpenClaw's native memory selector is a plugin slot
(`plugins.slots.memory`, switched by `openclaw plugins install …`, the
mechanism its LanceDB memory uses). A native `openclaw-memory-compartment`
slot plugin (auto-recall via the `before_prompt_build` hook, bridging to
the local compartment engine) is planned; the MCP path above works today.

## Everything else

| Agent kind | Mechanism | What to do |
|---|---|---|
| Any MCP-capable host (Cursor, Windsurf, custom SDK agents, …) | MCP stdio | the same one-block config as Claude, with its own `--caller` name |
| Python frameworks (LangChain/LlamaIndex-style) | vector-store adapter | import `compartment.vault.Vault` directly, or use the CLI; a drop-in `VectorStore` adapter class is on the roadmap |
| Anything that can run a subprocess | CLI/JSON | `compartment search "query" --json`, `compartment store "text"` |

One vault serves all of them at once: writes are serialized across
processes, every host gets its own `--caller` identity and namespace, and
the per-caller ACLs in `<vault>.config.json` decide who reads what.
