# Selecting Compartment as your agent's memory

How to make Compartment the active memory in each ecosystem. Four mechanisms
exist in the wild - a native provider slot (Hermes, OpenClaw), MCP tool
registration (Claude and most modern agents), agent-extension hooks (Oh My
Pi), and plain CLI/JSON (anything that can run a subprocess). Compartment
supports all four from one install.

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
    "command": "/Users/you/Library/Python/3.12/bin/compartment",
    "args": ["--vault", "/Users/you/.compartment/memory.vault",
             "--caller", "claude-desktop", "serve"] } } }
```

Use the absolute path of the `compartment` command (`which compartment`,
or `where compartment` on Windows), not the bare name: an app started from
the Dock or the Start menu does not get your shell's PATH, and a bare
`compartment` fails there with "spawn compartment ENOENT". `integrate`
writes the absolute path for you: the script pip installed (including the
`pip install --user` directory the install scripts use), or the link a
package manager keeps for it (Homebrew, pipx, uv), which survives
upgrades.

Where Claude Desktop keeps that file:

| Platform | File |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows (MSIX: the Store build and the current direct download) | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` (Store: `AnthropicPBC.Claude_fnn82j28hfe8t`) |
| Windows (older installer) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux (official beta, and community builds) | `~/.config/Claude/claude_desktop_config.json` |

An MSIX app reads its private copy under `Packages` once it has saved its
settings, and ignores `%APPDATA%\Claude` from then on; `integrate claude`
writes whichever one the installed app is reading. Quit Claude Desktop
completely before you run it (on Windows, from the tray icon), or quit
and reopen it straight after: a running Claude Desktop can write the
config it loaded at startup back over the file, dropping a server added
while it ran (anthropics/claude-ai-mcp#1150).

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
   `PostToolUse` hook into `~/.claude/settings.json` (`%USERPROFILE%\.claude`
   on Windows, or `$CLAUDE_CONFIG_DIR/settings.json` when that is set). When
   Claude Code writes a file into its memory directory (under `projects/`, or
   wherever its `autoMemoryDirectory` setting points), the hook stores it in
   the vault - no tool call required, so capture is deterministic instead of
   discretionary. Claude Code runs hooks through bash, Git Bash on Windows,
   so the hook names `compartment` by an absolute path written with forward
   slashes there.
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

## Oh My Pi (omp) - one command, plus an extension

omp is a local-first coding agent (TUI + CLI) with a full MCP client and an
extension system (`~/.omp/agent/extensions/`). Both paths work today.

```bash
pip install compartment && compartment init && compartment integrate omp
```

`integrate omp` writes the server entry under `mcpServers` in
`~/.omp/agent/mcp.json` (backing the file up first). A running session
picks it up with `/mcp reload`, which rediscovers servers and rebinds the
tools without a restart.

omp also reads a project-level `.omp/mcp.json`, relative to whichever
repository you are in. `integrate` never writes that one, because which
repository it means depends on where you were standing. Paste the block by
hand to wire the vault to a single project instead of the user-level file:

```json
{ "mcpServers": { "compartment": { "command": "compartment",
    "args": ["--vault", "~/.compartment/memory.vault",
             "--caller", "omp", "serve"] } } }
```

### The extension - deterministic read and write paths

The MCP entry leaves recall and capture to the model. The extension in
[`integrations/omp/`](../integrations/omp/README.md) adds the paths the
Claude Code integration gets from its hooks:

```bash
cp integrations/omp/extension.ts ~/.omp/agent/extensions/compartment.ts
```

`before_agent_start` recalls project memory automatically (DATA, not
instructions), user turns are buffered and flushed to the vault at session
shutdown and before compaction, and recalled memory is injected into the
compaction context so summaries do not drop prior decisions. `memory_search`
and `memory_store` are also registered as tools, independent of MCP wiring.

## Every other MCP client - `compartment integrate <name>`

`compartment integrate --list` shows every client below and whether it is
installed and connected here; `compartment integrate --all` wires every one
it finds. Each write keeps the client's other servers, takes a byte-exact
backup first (`<file>.compartment-backup`), follows a symlinked config to
the file it points at, and refuses - printing the block to paste instead -
anything it cannot read or edit safely. Files saved with a byte order mark
or as UTF-16 (Notepad, PowerShell 5) are read and written back as UTF-8.

| `integrate …` | File it writes (macOS / Linux; Windows where it differs) | Notes |
|---|---|---|
| `cursor` | `~/.cursor/mcp.json` | |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` | |
| `vscode`, `vscode-insiders` | `<User dir>/mcp.json`, key `servers` | comments kept |
| `zed` | `~/.config/zed/settings.json` (Linux: `$XDG_CONFIG_HOME/zed`; Windows: `%APPDATA%\Zed`), key `context_servers` | comments kept |
| `cline` | `~/.cline/data/settings/cline_mcp_settings.json` (`CLINE_DIR`, `CLINE_DATA_DIR`, `CLINE_MCP_SETTINGS_PATH`); older Cline: its editor's globalStorage | found in VS Code, Insiders, VSCodium, Cursor, Windsurf |
| `roo` | `<editor>/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json` | |
| `kilo` | `~/.config/kilo/kilo.jsonc` (or the kilo/opencode file already there), key `mcp` | comments kept |
| `continue` | `~/.continue/mcpServers/compartment.json` (`CONTINUE_GLOBAL_DIR`) | a file of its own; config.yaml untouched |
| `codex` | `~/.codex/config.toml` (`CODEX_HOME`), `[mcp_servers.compartment]` | re-running refreshes a stale entry |
| `gemini` | `~/.gemini/settings.json` (`$GEMINI_CLI_HOME/.gemini`) | comments kept |
| `qwen` | `~/.qwen/settings.json` (`QWEN_HOME`) | comments kept |
| `copilot-cli` | `~/.copilot/mcp-config.json` (`COPILOT_HOME`) | |
| `opencode` | `~/.config/opencode/opencode.json` or `.jsonc` (`OPENCODE_CONFIG_DIR`), key `mcp` | comments kept |
| `crush` | `~/.config/crush/crush.json` (`CRUSH_GLOBAL_CONFIG`), key `mcp` | |
| `kiro` | `~/.kiro/settings/mcp.json` (`KIRO_HOME`) | |
| `amazon-q` | `~/.aws/amazonq/mcp.json` | |
| `junie` | `~/.junie/mcp/mcp.json` | |
| `factory` | `~/.factory/mcp.json` | |
| `omp` | `~/.omp/agent/mcp.json` | |
| `lmstudio` | `<LM Studio home>/mcp.json`, from `~/.lmstudio-home-pointer` (default `~/.lmstudio`) | |
| `anythingllm`, `boltai`, `5ire`, `trae` | each app's own `mcp.json` | |
| `goose` | prints an `extensions:` block for `~/.config/goose/config.yaml` (Windows: `%APPDATA%\Block\goose\config\config.yaml`; `GOOSE_PATH_ROOT`) | paste, not written |
| `librechat`, `jan`, `cherry-studio`, `chatbox`, `msty`, `witsy`, `open-webui` | prints the block and where it goes | paste, not written |

"Comments kept" means the client reads its file as JSON with comments, so a
file holding `//` comments or trailing commas is edited as text: the entry
is spliced in, the result is read back and must hold exactly the old
settings plus the entry, and every comment stays where it was.

## Everything else

| Agent kind | Mechanism | What to do |
|---|---|---|
| Any other MCP-capable host (custom SDK agents, …) | MCP stdio | the same one-block config as Claude, with its own `--caller` name |
| Python frameworks (LangChain/LlamaIndex-style) | vector-store adapter | import `compartment.vault.Vault` directly, or use the CLI; a drop-in `VectorStore` adapter class is on the roadmap |
| Anything that can run a subprocess | CLI/JSON | `compartment search "query" --json`, `compartment store "text" --source "how you know"` |

One vault serves all of them at once: writes are serialized across
processes, every host gets its own `--caller` identity and namespace, and
the per-caller ACLs in `<vault>.config.json` decide who reads what.
