# How Compartment compares

What each project documents about where memory lives and what protects it,
checked against its own source or docs on 2 September 2026. "Not documented"
means the project's README and docs do not describe the property; it is not a
claim about undocumented behaviour. Corrections are welcome as a PR against
this file.

| | Memory at rest | Encrypted at rest | Account or API key | Network at runtime | License |
|---|---|---|---|---|---|
| **Compartment** | one AEAD-sealed vault file, index in RAM | **yes, vectors included** | none | none; a runtime guard aborts on any socket, enforced in CI on three OSes | Apache-2.0 |
| Claude Code built-in memory | Markdown under `~/.claude` and per project | not documented | none | none | proprietary host |
| Hermes Agent built-in memory | `MEMORY.md` (2,200 chars) + `USER.md` (1,375 chars), injected whole at session start [1] | not documented | none | none | MIT |
| OpenClaw built-in memory | `MEMORY.md` + daily `memory/YYYY-MM-DD.md` logs [2] | not documented | embedding provider for search | embedding calls | MIT |
| `@modelcontextprotocol/server-memory` | plaintext `memory.jsonl` inside the installed package directory; search is case-insensitive substring match [3] | no | none | none | MIT |
| mem0 (open source) | vector store + facts extracted by an LLM | not documented | an LLM key (OpenAI by default; local models configurable) | LLM calls per store; usage telemetry on by default [4] | Apache-2.0 |
| Graphiti (Zep) | Neo4j graph | not documented | an LLM key | LLM calls | Apache-2.0 |
| Letta | server + database | not documented | an LLM key | LLM calls | Apache-2.0 |
| claude-mem | local SQLite + Chroma | not documented | sign-in required (email magic link), subscription after a trial for the hosted observer [5] | account + provider calls | see project |
| basic-memory | Markdown files + SQLite index | not documented | none locally | none locally | AGPL-3.0 |

Sources

1. Hermes Agent docs, *Memory*: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
2. OpenClaw docs, *Memory*: https://open-claw.bot/docs/concepts/memory/
3. `src/memory/index.ts` in modelcontextprotocol/servers: `defaultMemoryPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'memory.jsonl')`; `searchNodes` filters with `toLowerCase().includes(...)`. https://github.com/modelcontextprotocol/servers/blob/main/src/memory/index.ts
4. `mem0/memory/telemetry.py`: `MEM0_TELEMETRY = os.environ.get("MEM0_TELEMETRY", "True")`, events sent to `https://us.i.posthog.com` with a project key in the source. https://github.com/mem0ai/mem0/blob/main/mem0/memory/telemetry.py
5. claude-mem README, *Quick start*: "asks you to sign in to claude-mem in your browser (email magic link ...) ... When the free trial ends, memory automatically falls back to your Anthropic plan unless you subscribe." https://github.com/thedotmack/claude-mem

What the table does not say

- Every project above is useful and several are far larger than Compartment.
  A Markdown file is the right memory for a lot of people; see the README.
- "Not documented" is the honest cell for a property nobody claims. If a
  project encrypts its store and we missed it, open a PR.
- Embedding vectors are not anonymous: text can be substantially
  reconstructed from its embedding (Morris et al., *Text Embeddings Reveal
  (Almost) As Much As Text*, 2023, https://arxiv.org/abs/2310.06816). That is
  why Compartment encrypts the vectors, not only the text.
