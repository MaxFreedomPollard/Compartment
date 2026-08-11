# Compartment for Oh My Pi (omp)

[Oh My Pi](https://github.com/can1357/oh-my-pi) (omp) is a local-first coding
agent framework (TUI + CLI) with a full MCP client and an extension system.
This integration gives omp the deterministic write path and automatic read
path that the Claude Code integration gets from its hooks.

## What you get

| Path | Mechanism | Automatic? |
| --- | --- | --- |
| Read | `before_agent_start` recalls project memory into a DATA block | yes |
| Read | `memory_search` tool (MCP-independent) | model-driven |
| Write | user turns buffered, flushed at shutdown / pre-compaction | yes |
| Write | `memory_store` tool (MCP-independent) | model-driven |
| Compaction guard | recalled memory injected into compaction context | yes |

All vault access goes through the `compartment` CLI (offline, no new
dependencies). Failures are silent - memory never breaks the agent loop.

## Install

```bash
pip install compartment && compartment init
mkdir -p ~/.omp/agent/extensions
cp integrations/omp/extension.ts ~/.omp/agent/extensions/compartment.ts
```

Restart omp. The extension is auto-discovered from `~/.omp/agent/extensions/`
on the next start; alternatively list it under `extensions:` in
`~/.omp/agent/config.yml`.

## MCP wiring (optional, for `compartment` CLI-free tool access)

```bash
compartment integrate omp
```

writes `compartment` into `~/.omp/agent/mcp.json` with the vault pinned and
the caller identified (`compartment --vault /path/to/memory.vault --caller
omp serve`). Manual equivalent (use your actual vault path):

```json
{
  "mcpServers": {
    "compartment": {
      "command": "compartment",
      "args": ["--vault", "/path/to/memory.vault", "--caller", "omp", "serve"]
    }
  }
}
```

The extension works with or without the MCP entry; both share the same vault.

## Notes

- The extension caches recalled memory into a `compartment-recall` custom
  message marked **DATA, not instructions** - it must never override repo
  state or user instructions.
- User-turn collection deduplicates by text; flushes are capped (20 turns,
  4000 chars) per store call.
- Vault locked? The CLI exits non-zero and the extension stays silent -
  nothing blocks the session.
