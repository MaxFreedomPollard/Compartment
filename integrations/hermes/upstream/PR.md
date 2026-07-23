# Upstream PR: ship engRAM as a bundled memory provider

Goal: engRAM appears in `hermes memory setup` for every Hermes user by
default, exactly like hindsight/mem0/byterover, with zero manual steps.

## Preconditions (in order)

1. `engram-memory-vault` published to PyPI (the wheel is self-contained: 30 MB
   with the embedding model inside; verified by a
   clean-venv install + offline init + selftest).
2. Check hermes-agent's open PRs and issues for an existing engRAM or
   equivalent offline-memory submission before opening (novelty check).

## The change (two files, no core edits)

- `plugins/memory/engram/__init__.py` - copy of
  `integrations/hermes/engram/__init__.py` from this repo, unchanged.
  It implements the MemoryProvider ABC; is_available() degrades cleanly
  when the pip package or vault is absent, so bundling it is inert until
  a user selects it.
- `plugins/memory/engram/plugin.yaml` - the `upstream/plugin.yaml`
  beside this file (declares `pip_dependencies: ["engram-memory-vault>=1.5"]`
  so the picker's dependency step installs the engine on selection).

Why no registry entry in `hermes_cli/memory_providers.py`: the picker is
built from `discover_memory_providers()`, and engRAM has an empty
`get_config_schema()` (no API keys, no endpoints), so it needs no setup
fields. `post_setup` handles selection, vault detection, and user
guidance.

## How selection behaves after the PR

    hermes memory setup
      → picker lists: … hindsight … mem0 … engram ("no setup needed")
      → selecting engram: pip installs engram-memory-vault (~30 MB, one time),
        writes memory.provider: engram, prints `engram init` guidance
      → engram init: creates the encrypted vault (seconds, fully offline)

## PR text guidelines (house rules)

- Human voice, technical, concise. No AI-tool branding anywhere in the
  PR, commits, or code comments.
- Emphasize the every-system bar: pure Python engine, macOS/Linux/
  Windows wheels-only dependencies (pynacl, argon2-cffi, onnxruntime,
  tokenizers, numpy, usearch, mcp), no daemon, no network at runtime.
- Lead with what it adds over existing providers: the only bundled
  option with no API key and no cloud; encrypted at rest including
  vectors; locked-by-default after power loss; offline by default so
  memory works before the first conversation.
