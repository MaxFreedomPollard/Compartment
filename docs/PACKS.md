# Authoring Memory Packs

A memory pack is a signed, versioned `.mpack` file of knowledge that
installs into any Compartment vault fully offline, with precomputed vectors.

## Build

1. Prepare records - `.jsonl` (one `{"text": "...", "tags": [...],
   "id": "stable-id"}` per line), `.csv` (columns `text`, `tags`
   semicolon-separated), or a directory of `.md` files (one record per
   file).
2. Build and sign:

```bash
compartment pack build facts.jsonl \
    --name my-pack --version 1.0.0 \
    --description "What this pack knows" \
    --creator "Your Name" \
    --identity ~/.compartment/identity.json      # created on first use - KEEP PRIVATE
```

The build embeds every record with the bundled default model, so consumers
install with zero compute. `--encrypt` additionally seals the body with a
pack passphrase (private distribution).

3. Distribute the `.mpack` file however you like - signature and content
   hash make tampering in transit detectable. Tell your users your public
   key (`pub_hex` in your identity file) over a channel they trust: they
   need it to install your pack (see **Trust**, below).

## Consume

```bash
compartment pack install my-pack-1.0.0.mpack     # verifies signature + hash FIRST
compartment pack list
compartment pack remove my-pack
```

Records land read-only in `packs/my-pack` for every caller. Reinstalling a
pack replaces it wholesale (semver replace, never merge). A pack signed by
anyone other than the Compartment project only installs once you name the
author's key yourself - see **Trust**, below.

## Trust: a pack cannot vouch for itself

Every pack carries a `signer_pub` field. That field is a **hint only** and is
never used to verify anything - a pack that supplied its own verification key
would prove nothing, since anyone can mint a keypair and sign a malicious
pack with it.

Verification resolves the key from a **trusted set** instead, in this order:
signature (trusted key) → content hash → decrypt → parse.

- The only key trusted by default is the Compartment project's own verify
  key, `packs.PROJECT_PACK_KEY` in `src/compartment/packs.py`. It ships in
  the source tree so the bundled starter pack can be verified without
  trusting the file it arrived in.
- Anything else is refused, loudly, with the untrusted key printed.

To install a third-party pack, get the author's public key over a channel you
trust (their site over HTTPS, a signed release, in person - not from the pack
itself) and name it deliberately:

```python
from compartment import packs
packs.install_pack(vault, blob, trusted_keys=["<64-hex author key>"])
packs.read_pack(blob, trusted_keys=["<64-hex author key>"])   # inspect only
```

`trusted_keys` accepts one hex key or a list, applies to that call only, and
adds to the project key rather than replacing it. Naming a key is the whole
consent step: if you have not verified where the key came from, you have not
verified the pack.

### Signing a rebuild (maintainers)

The project's Ed25519 **private** seed lives in `tools/pack_identity.json`,
which is gitignored and must never be committed, copied into the source tree,
or pasted anywhere. `tools/build_starter_pack.py` reads it to sign the pack.
Only the public half (`pub_hex`) belongs in `packs.py`; the two must match or
the shipped pack stops verifying. If that file is lost, packs signed by the
old key can no longer be produced; if it leaks, rotate the keypair and
publish `PROJECT_PACK_KEY` afresh.

## Rules of the road

- **Keep `identity.json` private.** It contains your Ed25519 seed. Publish
  only the public key (`pub_hex`), and publish it somewhere consumers can
  check independently of the pack: the copy inside the pack is a hint and
  buys them nothing.
- **Stable `id` fields** let you ship regression queries against your pack
  (the ids survive install as `id:` tags - this is exactly how the
  built-in starter memories' `selftest` works).
- **Never change published content within a version.** Version bumps are
  cheap; silent mutations defeat the signature's purpose.
- Facts should stand alone (one self-contained statement per record) -
  retrieval returns records, not documents.

## Built-in starter memories (maintainer notes)

Compartment ships a signed starter pack whose contents are SEEDED at `init` -
verified like any pack (signature + content hash), then stored as
ordinary, fully editable memories in the `main` namespace. There is no
separate read-only starter section: starting memories sit beside (and
behave exactly like) everything the agent stores later. Vaults created
by older versions that still have a `packs/starter` section are
reorganized automatically the next time they open - every record moves
to `main` untouched. Canonical hand-editable source:

`tools/starter/starter_facts.jsonl` - one JSON object per line,

```json
{"id": "akc-00001", "tags": ["akc", "measurements"], "text": "The body mass of an African elephant (adult) is typically about 6,000 kg (ranging from 4,000 to 7,000 kg)."}
```

### Editing the starter memory

1. Edit `tools/starter/starter_facts.jsonl` in any editor: insert, delete,
   reword, append. Line position is irrelevant to retrieval; only ids must
   stay unique. Keep ids `core-001`..`core-260` textually intact (they are
   the frozen `compartment selftest` corpus).
2. Rebuild (re-embeds every line with the bundled model and re-signs):
   ```bash
   python tools/build_starter_pack.py 1.0.1     # arg = new pack version
   ```
3. Every future `compartment init` then seeds your edited facts (into `main`,
   as ordinary editable memories). To refresh an EXISTING vault, export
   your organic memories, init a fresh vault (which seeds the new
   starter), and import them back:
   ```bash
   compartment export mine.jsonl --plaintext
   compartment --vault ~/.compartment/memory2.vault init
   compartment --vault ~/.compartment/memory2.vault import mine.jsonl
   compartment --vault ~/.compartment/memory2.vault selftest   # must stay 20/20
   ```
   (Then shred `mine.jsonl` - it is plaintext.) Already-present facts
   simply coexist; forget the ones you replaced.

The AKC-derived section can be regenerated from upstream with
`tools/build_akc_pack.py` (writes `tools/starter/akc_regenerated.jsonl`
for merging). Any other pack can be dumped for editing with
`compartment pack export <file.mpack> <out.jsonl>` and rebuilt with
`compartment pack build`.
