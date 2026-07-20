# How engRAM Remembers

How memory is stored, how engRAM decides what becomes a memory, and why
this design is mathematically stronger than the alternatives. Every claim
in this document is enforced by code and covered by the test suite.

---

## 1. How a memory is stored

When an agent finishes a turn (or calls `engram_store` / `memory_store`),
the following happens **locally, in about 40 milliseconds, with zero
network involvement**:

```
text
 │  1. EMBED         bundled bge-small int8 ONNX model, on your CPU
 ▼                   → one 384-dimensional unit vector (~25 ms)
vector + text
 │  2. DEDUPLICATE   nearest-neighbor cosine ≥ 0.97 in the same namespace
 ▼                   → return the existing record instead of double-storing
record
 │  3. ENCRYPT       fresh per-record key wraps the text (XChaCha20-Poly1305);
 ▼                   the per-record key itself is wrapped by the vault master
 │                   key - destroying it later = crypto-shred
 │  4. INDEX         vector → RAM-resident index (exact SIMD < 20k records,
 ▼                   SIMD HNSW above); tokens → FTS5 BM25 index
 │  5. PERSIST       the write is sealed (AEAD) and appended to the vault
 ▼                   journal with fsync - an acknowledged write survives
 │                   kill -9 and power loss
 └─ 6. AUDIT         a hash-chained, encrypted audit entry records who
                     stored what, when - history cannot be silently edited
```

Every stored record carries: the text (encrypted under its own key), its
384-dim float32 vector, namespace, tags, importance, quarantine flag,
provenance (host / agent / session), and timestamps. **The vector is
encrypted at rest too** - embeddings can be partially inverted back toward
their text, so treating vectors as non-sensitive (as most systems do) is a
hole; engRAM doesn't have it.

New memories land in the writing agent's namespace (e.g. `hermes` or
`main`), in the same vault and the same searchable index - one vector
space, searched together. The starting memories engRAM seeds at `init`
live in `main` too, as ordinary records: searchable, taggable, editable,
forgettable, exactly as if the agent had stored them (vaults created by
older versions are reorganized this way automatically on open). Optional
add-on packs are the one exception: they install read-only under
`packs/<name>`, so separately curated content is never diluted by chat
traffic.

Alongside records, the vault keeps a **memory graph**: explicit
subject-predicate-object relations (`memory_link` / `engram link`),
optionally tied to the memory they came from and to a validity window,
queryable by entity, predicate, or as-of time (`memory_relations`).
Storage and matching are deterministic (case-insensitive entity
normalization, idempotent re-links); the judgment of what to link belongs
to the host model, exactly like memory curation. `engram dash` draws the
graph and the rest of the vault's shape in the browser - served from RAM,
loopback-only, read-only.

### Nothing embeds twice

Runtime embedding happens exactly once per new memory, forever cached
inside the vault under the pinned model. Optional signed packs can ship
precomputed vectors bit-exact for that model (see [PACKS.md](../PACKS.md));
that is an authoring path, not something you configure day to day.

## 2. How engRAM decides what becomes a memory

engRAM never calls an LLM (that is what keeps the offline guarantee
absolute), so the write decision is a **deterministic, importance-tiered
classifier**. Its governing principle: **remember aggressively, and
prioritize the user and their machine over world trivia.** The user is not
here to have their words filtered - *every answer they give is
information.*

**Store nearly everything.** The only turns dropped are genuinely empty
ones. A bare "yes", "no", or "OK" is not noise - it is a decision, and
often the most important thing in the whole session. When the assistant
asks *"Want me to send this reply to the client now?"* and the user
replies *"OK"*, engRAM stores a self-contained consent record -
`[decision <date>] Approved (answered "OK"): Want me to send this reply
to the client now?` - by resolving the question from the preceding
assistant turn. A later "did the user say to email the client?" retrieves
exactly that.

**Importance tiers drive ranking, so completeness never buries what
matters.** Every stored memory gets a deterministic importance:

| Importance | What |
|---|---|
| 0.90 | decisions / consent / explicit "remember this" |
| 0.80 | personal facts and preferences (about the user) |
| 0.75 | the user's machine / environment / configuration |
| 0.55 | other substantive statements (incl. world facts the user states) |
| 0.20 | pure social pleasantries ("thanks", "lol") - kept, ranked last |

Classification is by signal: personal pronouns and preference verbs
(*I, my, prefer, favorite, always, remember*) mark tier-0.80 facts; system
vocabulary (*registry, path, password, port, install, version, sudo,
service*) marks tier-0.75 machine facts. Nothing is discarded for being
low-value - pleasantries are simply ranked last.

**Deduplication is exact-match only.** Vault.store drops a write whose
nearest neighbor is ≥ 0.97 cosine (a literal repeat) and returns the
existing id. There is deliberately **no aggressive novelty gate**: a second
"yes" to a *different* question is new information, and because its stored
text embeds the question it is not a near-duplicate anyway. engRAM favors
completeness over compactness - at a few KB per memory, thousands of turns
cost only a few MB, and importance-weighted ranking keeps recall sharp.

**Recall reflects the priority.** The fused search score is
`RRF(vector) + RRF(keyword) + 0.02·cosine + 0.006·importance`. The cosine
term preserves the *magnitude* of a strong match (so the best answer wins
outright); the importance term is a gentle nudge that lets a
personal/decision memory win a genuine near-tie without overriding a
clearly better match. In practice: ask "what theme does the user like?"
and the dark-mode preference wins on relevance; ask "did the user say to
email the client?" and the consent decision wins.

**The agent-directed path.** The host model (Hermes, Claude, anything)
also holds `engram_store` / `engram_forget` tools, so the intelligence
*you already pay for* can curate explicitly - distilling, correcting, or
crypto-shredding memories. engRAM splits the labor: **the host model
supplies judgment; engRAM supplies deterministic capture, encryption, and
total recall.** A memory layer that runs its own LLM either phones home
(privacy gone) or ships a second multi-gigabyte model (your RAM gone) - and
its decisions become non-reproducible either way.

Recall closes the loop: before each turn the user's message is embedded
and the top memories above cosine 0.45 are injected as context, ranked by
reciprocal-rank fusion of vector similarity and BM25 keyword score.
Recalled content is always wrapped with a *data-not-instructions* notice,
and memories flagged `quarantined` (stored from untrusted sources) carry
an explicit warning envelope - stored-prompt-injection hygiene that
memory layers generally lack.

## 3. Why this is mathematically and logically stronger

**Exact search at personal scale - retrieval is provably optimal.** Below
20,000 records engRAM computes all similarities with SIMD matrix math:
the top-k result is the true top-k, recall = 1.0 by construction, in
under a millisecond. Mainstream vector stores run approximate (ANN)
indexes at *every* scale, accepting 95-99% recall on corpora small enough
to search exactly - approximation error with no compensating benefit.
engRAM only switches to HNSW (~99% recall, p95 < 15 ms at 1M) when the
corpus actually demands it, and `reindex` rebuilds from stored vectors at
any time.

**One pinned embedding space - cosine stays meaningful.** Similarity
between two vectors is only defined if one model produced both. engRAM
records the embedding model's SHA-256 in the vault and **refuses to open**
with a mismatched model (explicit `reindex --re-embed` migrates instead).
Systems that let the embedding model drift silently corrupt every
similarity they compute afterward - the errors are invisible until
retrieval quietly degrades.

**Completeness with clean recall.** engRAM stores nearly every turn (only
exact-duplicate writes are dropped), so nothing the user says is lost -
but importance tiers and cosine-weighted fusion keep retrieval sharp, so a
complete store does not mean a noisy recall. Turn-logging systems also grow
O(turns) but rank purely by recency or raw keyword match, so their
retrieval drowns in whatever the user discusses most; engRAM ranks by
relevance-plus-importance, surfacing decisions and personal facts first.

**The write path is crash-proof by argument, not by luck.** An
acknowledged write is an fsync'd, AEAD-sealed journal entry; compaction is
write-temp → fsync → atomic rename. A `kill -9` at any instant loses at
most the single unacknowledged write in flight (verified by test). A
truncated tail is distinguishable *cryptographically* from tampering, so
crash recovery can be lenient exactly where malice is impossible.

**Security reduces to standard hard problems.** Forging any stored byte
means defeating Poly1305 authentication (success probability ≈ 2⁻¹²⁸ per
attempt); recovering a shredded memory means decrypting XChaCha20 without
its destroyed key; brute-forcing the passphrase costs 64 MiB × 3
iterations of Argon2id *per guess*; reviving an unlock credential after
power loss means re-deriving a key whose input (the boot timestamp) no
longer exists. No policy checks, no honor system - each guarantee is an
inequality about a well-studied primitive.

**Latency arithmetic.** Local embed ~25 ms + exact search <1 ms ≈ **one
network round-trip is already slower than our entire pipeline.** Cloud
memory APIs add tens to thousands of milliseconds per recall and per
store, on every single turn, plus an availability dependency and a
privacy surrender. The RAM-residency that makes engRAM secure (no
plaintext index on disk) is the same property that makes it fast - in
this design, security and speed are the same decision.

**Honest limits** (see SECURITY.md for the full threat model): importance
classification is heuristic, not semantic understanding - the host model's
explicit curation exists precisely to cover what heuristics miss; storing
nearly everything trades disk for completeness by design; HNSW above 20k
records is ~99% recall, not 100%; and no memory system can protect an
unlocked vault from a fully compromised OS.

## 4. The same math, in one table

| Property | engRAM | Cloud memory APIs | Typical local vector DB | Raw turn-logging |
|---|---|---|---|---|
| Recall correctness (personal scale) | exact, 1.0 | ANN ≈0.95-0.99 | ANN ≈0.95-0.99 | keyword only |
| Store/recall latency | ~25 ms / <1 ms | 100 ms-40 s | ms | ms |
| Storage & ranking | complete store, importance-ranked recall | O(turns), recency-ranked | O(turns) | O(turns), keyword-ranked |
| Embedding-space consistency | hash-pinned, enforced | provider may change | unenforced | n/a |
| At-rest encryption (incl. vectors) | always, AEAD | provider-side | usually none | none |
| Deletion | crypto-shred | request + trust | row delete | row delete |
| Tamper evidence | AEAD + hash-chained audit | none visible | none | none |
| Works offline / air-gapped | always | never | usually | yes |
| Survives restart unlocked | no - locked by default | n/a | yes (no lock at all) | yes |
