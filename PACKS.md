# Authoring Memory Packs

A memory pack is a signed, versioned `.mpack` file of knowledge that
installs into any engRAM vault fully offline, with precomputed vectors.

## Build

1. Prepare records - `.jsonl` (one `{"text": "...", "tags": [...],
   "id": "stable-id"}` per line), `.csv` (columns `text`, `tags`
   semicolon-separated), or a directory of `.md` files (one record per
   file).
2. Build and sign:

```bash
engram pack build facts.jsonl \
    --name my-pack --version 1.0.0 \
    --description "What this pack knows" \
    --creator "Your Name" \
    --identity ~/.engram/identity.json      # created on first use - KEEP PRIVATE
```

The build embeds every record with the bundled default model, so consumers
install with zero compute. `--encrypt` additionally seals the body with a
pack passphrase (private distribution).

3. Distribute the `.mpack` file however you like - signature and content
   hash make tampering in transit detectable.

## Consume

```bash
engram pack install my-pack-1.0.0.mpack     # verifies signature + hash FIRST
engram pack list
engram pack remove my-pack
```

Records land read-only in `packs/my-pack` for every caller. Reinstalling a
pack replaces it wholesale (semver replace, never merge).

## Rules of the road

- **Keep `identity.json` private.** It contains your Ed25519 seed. The
  public key travels inside each pack; consumers can pin it.
- **Stable `id` fields** let you ship regression queries against your pack
  (the ids survive install as `id:` tags - this is exactly how the
  built-in seed pack's `selftest` works).
- **Never change published content within a version.** Version bumps are
  cheap; silent mutations defeat the signature's purpose.
- Facts should stand alone (one self-contained statement per record) -
  retrieval returns records, not documents.

## Built-in starter memories (maintainer notes)

engRAM ships a signed starter pack whose contents are SEEDED at `init` -
verified like any pack (signature + content hash), then stored as
ordinary, fully editable memories in the `main` namespace. There is no
separate read-only starter section: starting memories sit beside (and
behave exactly like) everything the agent stores later. Vaults created
by older versions that still have a `packs/starter` section are
reorganized automatically the next time they open - every record moves
to `main` untouched. Canonical hand-editable source:

`tools/starter/starter_facts.jsonl` - one JSON object per line,

```json
{"id": "akc-00001", "tags": ["akc", "measurements"], "text": "The body mass of a african elephant (adult) is typically about 6,000 kg (ranging from 4,000 to 7,000 kg)."}
```

### Editing the starter memory

1. Edit `tools/starter/starter_facts.jsonl` in any editor: insert, delete,
   reword, append. Line position is irrelevant to retrieval; only ids must
   stay unique. Keep ids `core-001`..`core-260` textually intact (they are
   the frozen `engram selftest` corpus).
2. Rebuild (re-embeds every line with the bundled model and re-signs):
   ```bash
   python tools/build_starter_pack.py 1.0.1     # arg = new pack version
   ```
3. Refresh an existing vault and verify (seeds into `main` as ordinary
   memories; already-present facts simply coexist - forget old ones if
   you replaced them):
   ```bash
   engram import <(python - <<'PY'
   # or simply create a fresh vault; seeding happens at init
   PY
   )
   engram selftest      # must stay 20/20
   ```
   The simplest refresh is a fresh `engram init` on a new vault path,
   then `engram import` of your exported organic memories.
Every future `engram init` then includes your edits.

The AKC-derived section can be regenerated from upstream with
`tools/build_akc_pack.py` (writes `tools/starter/akc_regenerated.jsonl`
for merging). Any other pack can be dumped for editing with
`engram pack export <file.mpack> <out.jsonl>` and rebuilt with
`engram pack build`.
