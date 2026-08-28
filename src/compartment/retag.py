"""Keeping tags true as the world moves, offline and without an LLM.

THE PROBLEM
-----------
A tag is written once, at the moment a memory is stored, by whoever was in the
room. What a memory is ABOUT does not change. What it is RELEVANT TO changes
constantly. A preference recorded while setting up a Zoho mailbox gets tagged
"zoho"; the preference outlives Zoho. Two years later the same preference is
the thing you need while setting up its replacement, and it is filed under a
company you no longer use. The memory is not wrong and does not want editing.
Its index entry is stale.

So tags have to be re-derived from the vault as it stands today, repeatedly,
in the background, without anyone asking - and without touching a single
character of what was actually remembered.

WHAT THIS IS ALLOWED TO TOUCH
-----------------------------
The `tags` column. Nothing else, ever. Not the text, not the ciphertext, not
the embeddings, not `created`, not importance, not the audit chain's account of
what happened. A retag pass that lost a memory, or quietly changed what one
said, would be a far worse failure than any stale tag it fixed, so the write
path is deliberately one line wide: Store.set_tags, which can only write that
one column. `tags_origin` keeps the creation-time tags forever, so every pass
is reversible and "what did we think this was about at the time" stays a
question with an answer.

THREE SIGNALS, NO MODEL
-----------------------
Compartment has never called an LLM and does not start here. Everything below
is computed from what the vault already contains.

1. SEMANTIC PROPAGATION. Every record already carries an embedding. A record's
   nearest neighbours are, by construction, the memories about the same thing;
   each neighbour votes for its own tags with its cosine as the weight, and a
   tag carrying enough of the total vote is attached. This is what actually
   fixes the Zoho case: when memories about the replacement arrive, they land
   next to the old preference in embedding space and lend it their tag. No
   rule anywhere names either company.

2. CO-OCCURRENCE IMPLICATION. Across the whole vault, some tags travel
   together: if nearly every record tagged A is also tagged B, then A implies B
   and a record carrying only A is under-tagged. This is association-rule
   confidence, P(B|A), and it is the signal that spreads a NEW vocabulary
   backwards over an old one during the window when both are in use.

3. VOCABULARY MATCH. A tag already in use somewhere in the vault, whose phrase
   appears literally in this record's text, belongs on this record. Restricting
   the lexical signal to the EXISTING tag vocabulary is what keeps it clean:
   mining free text for "distinctive terms" invents a new label vocabulary out
   of typos, hostnames and one-off nouns, and a tag nobody will ever search for
   is just noise with a schema.

ADDITIVE BY DEFAULT
-------------------
A pass adds tags and does not remove them unless asked (`prune=True`). Removal
is the only genuinely lossy thing here, and the asymmetry is not laziness: a
wrong tag added costs one irrelevant hit in a filtered search, while a right
tag removed costs a memory that can no longer be found the way its owner
remembers filing it. Pruning, when enabled, still refuses to drop a tag whose
phrase is present in the record's own text.

WHY IT CANNOT PROPAGATE AN IDENTITY
-----------------------------------
Seeded starting memories are told apart from learned ones by an "id:" tag, and
that single tag decides what `memory_recent` shows and whether the
starter-facts filter can see a record at all (vault.is_seeded). Those tags are
unique to one record by definition, so a propagation pass that treated them as
ordinary labels would smear one seeded fact's identity across its whole
neighbourhood and silently reclassify thousands of memories as starter content.
They are excluded from the vote, never proposed, and re-added on write by
Store.set_tags. This is the single most dangerous thing a retagger could do to
this vault, so it is blocked in three places rather than one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import numpy as np

from .store import Store

# --- what counts as a neighbour ----------------------------------------------
# Cosines from an L2-normalized encoder are comparable across queries, so a
# fixed floor is meaningful. 0.55 is well above the ~0.25 where this encoder's
# similarities stop meaning anything and below the ~0.7 where records are near
# paraphrases; a tag is worth borrowing from a memory about the same SUBJECT,
# not only from one that restates it.
NEIGHBOUR_FLOOR = 0.55
# Enough voters that one oddly-tagged neighbour cannot carry a tag on its own,
# few enough that the neighbourhood is still about one thing.
NEIGHBOURS = 12

# --- how much support a tag needs --------------------------------------------
# Share of the total neighbour weight a tag must carry to be attached. At 0.34
# a tag needs roughly a third of the neighbourhood behind it, which one
# neighbour cannot reach while three agreeing ones clear it easily.
PROPAGATE_SHARE = 0.34
# P(B|A) at which tag A is taken to imply tag B.
IMPLICATION_CONFIDENCE = 0.80
# A implies B is only worth believing once A has been used enough times for the
# ratio to mean anything. Below this, one coincidence is a "rule".
IMPLICATION_MIN_SUPPORT = 5
# A tag proposed by the lexical signal must be at least this long, so that
# two-letter tags do not match inside unrelated words.
VOCAB_MIN_LEN = 3
# No record gets an unbounded pile of tags. Highest-scoring first.
#
# Measured, and lowered from 12: against a real 7,206-record vault every single
# record saturated a 12-tag ceiling. A ceiling everything reaches is not a
# ceiling, it is a quota, and a memory wearing twelve tags is no more findable
# than one wearing none - the filter stops selecting anything.
MAX_TAGS_PER_RECORD = 8

# --- what is even eligible to be proposed --------------------------------------
# A tag has to be a CATEGORY before it can be spread. These three filters are
# what separate a label somebody once typed from a subject the vault actually
# organizes itself around, and without them propagation degrades into spam.
#
# 1. Used by at least this many records already. A tag on one memory is that
#    memory's private label - "max-automation-philosophy" names a specific note,
#    it does not describe a class of them - and copying it onto a neighbour
#    asserts a category that has never existed.
MIN_TAG_SUPPORT = 3
# 2. Not on more than this share of the vault. A tag carried by a quarter of
#    everything cannot narrow a search, so attaching it to more records costs
#    context and buys nothing. Same reasoning as ranking.COMMON_TERM_FRACTION:
#    the ceiling is measured from this vault, not from a fixed list.
MAX_TAG_FRACTION = 0.25
# ...but never below this many records. A fraction alone is meaningless on a
# small vault: in a vault of six memories a tag shared by four is 67% and would
# be excluded as "uninformative" when it is in fact the only category there is.
# A tag on twenty records is a real category whatever the vault size, so the
# ceiling is the LARGER of the two.
MIN_COMMON_ABSOLUTE = 20
# 3. Not a date. A date-shaped tag marks WHEN a memory was written, which is
#    already a column and is exactly what must not travel: propagating
#    "2026-07-26" onto a neighbour claims that neighbour was about that day.
DATE_TAG_RE = re.compile(r"^\d{4}(-\d{1,2}){0,2}$")

# --- pruning (off unless asked) ----------------------------------------------
# Below this share of neighbour support an existing tag is considered
# unsupported. Deliberately far below PROPAGATE_SHARE: the bar to keep a tag a
# human chose is much lower than the bar to invent one.
PRUNE_SHARE = 0.05
# Blocked in matmul-sized pieces: a full pairwise similarity matrix over a large
# vault is quadratic in memory, and the neighbourhood of each row is all that is
# ever needed.
BLOCK = 512


@dataclass
class Change:
    """One record's proposed retag. `added` and `removed` are what a person
    reviewing a dry run actually wants to see."""
    record_id: str
    before: list[str]
    after: list[str]
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def is_change(self) -> bool:
        return bool(self.added or self.removed)


def _descriptive(tags: list[str]) -> list[str]:
    """Tags a person would recognise as a subject label.

    Structural tags are filtered out of every vote, every proposal and every
    score. They identify a record; they do not describe one."""
    return [t for t in tags if not t.startswith(Store.PROTECTED_TAG_PREFIXES)]


def _record_matrix(db: Store) -> tuple[list[str], np.ndarray]:
    """One unit vector per RECORD, from the per-window vectors.

    A long record is stored as several overlapping windows. Averaging them and
    renormalizing gives the record one position in embedding space, which is
    what a neighbourhood needs: taking only the first window would file a long
    memory by its opening paragraph, and treating every window as its own point
    would let a single wordy record outvote a dozen concise ones."""
    ids, _ikeys, mat = db.all_vectors()
    if mat.size == 0:
        return [], np.zeros((0, 0), dtype=np.float32)
    order: list[str] = []
    sums: dict[str, np.ndarray] = {}
    for rid, vec in zip(ids, mat):
        if rid not in sums:
            sums[rid] = np.zeros(mat.shape[1], dtype=np.float32)
            order.append(rid)
        sums[rid] += vec
    out = np.vstack([sums[r] for r in order])
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return order, out / norms


def _implications(tagsets: list[list[str]]) -> dict[str, dict[str, float]]:
    """{tag A: {tag B: P(B|A)}} for rules clearing the support and confidence
    bars. Computed once per pass over the whole vault."""
    counts: dict[str, int] = {}
    pairs: dict[tuple[str, str], int] = {}
    for tags in tagsets:
        uniq = sorted(set(tags))
        for a in uniq:
            counts[a] = counts.get(a, 0) + 1
            for b in uniq:
                if a != b:
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1
    rules: dict[str, dict[str, float]] = {}
    for (a, b), n in pairs.items():
        support = counts.get(a, 0)
        if support < IMPLICATION_MIN_SUPPORT:
            continue
        conf = n / support
        if conf >= IMPLICATION_CONFIDENCE:
            rules.setdefault(a, {})[b] = conf
    return rules


def eligible_tags(desc_by_id: dict[str, list[str]]) -> set[str]:
    """The tags this pass is allowed to ADD to a record.

    Nothing here restricts what a memory may KEEP. A tag a person chose stays
    on the memory they chose it for however rare or specific it is; this only
    governs what may be copied onto a memory that does not have it yet."""
    counts: dict[str, int] = {}
    for tags in desc_by_id.values():
        for t in set(tags):
            counts[t] = counts.get(t, 0) + 1
    total = max(1, len(desc_by_id))
    ceiling = max(MIN_COMMON_ABSOLUTE, total * MAX_TAG_FRACTION)
    return {t for t, n in counts.items()
            if n >= MIN_TAG_SUPPORT
            and n <= ceiling
            and not DATE_TAG_RE.match(t)}


def _vocab_pattern(vocab: set[str]) -> dict[str, re.Pattern]:
    """A word-boundary matcher per known tag.

    Word boundaries matter: without them the tag "go" matches inside "Google"
    and every memory mentioning it acquires a language."""
    out = {}
    for tag in vocab:
        if len(tag) < VOCAB_MIN_LEN:
            continue
        out[tag] = re.compile(r"\b" + re.escape(tag).replace(r"\ ", r"\s+") + r"\b",
                              re.IGNORECASE)
    return out


def targets(vault, *, include_seeded: bool = False) -> list[str]:
    """The record ids a pass would consider, in a stable order.

    Exposed so a caller can retag in slices. The background pass has to: it
    shares one lock with every memory tool, and on a large vault a single
    whole-vault pass would hold that lock for a minute and a half, during which
    every search an agent makes simply stops. Bookkeeping must never be
    something a user can feel."""
    from .vault import is_seeded
    out = []
    # Tombstones are excluded: retagging a record no search can return is
    # lock time spent on nothing.
    for row in vault.db.conn.execute(
            "SELECT id, tags FROM records WHERE superseded_by IS NULL"
            " ORDER BY id"):
        if include_seeded or not is_seeded(row["tags"]):
            out.append(row["id"])
    return out


def plan(vault, *, include_seeded: bool = False, prune: bool = False,
         limit: int | None = None, only: list[str] | None = None) -> list[Change]:
    """Work out what every record's tags should be. Writes nothing.

    `include_seeded` is off because a vault's starting memories are curated
    pack content whose tags were chosen deliberately; the memories worth
    re-deriving are the ones this machine learned. Seeded records still VOTE -
    they are context, and excluding them from the neighbourhood would throw
    away most of what the vault knows.
    """
    from .vault import is_seeded            # local: vault imports this module

    db = vault.db
    ids, mat = _record_matrix(db)
    if not ids:
        return []
    index = {rid: i for i, rid in enumerate(ids)}

    rows = {}
    for row in db.conn.execute("SELECT id, tags FROM records"):
        rows[row["id"]] = row["tags"]

    tags_by_id = {rid: json.loads(t) for rid, t in rows.items()}
    desc_by_id = {rid: _descriptive(t) for rid, t in tags_by_id.items()}
    rules = _implications([v for v in desc_by_id.values() if v])
    # One eligibility set, applied to every way a tag can be proposed. Filtering
    # in only one of the three signals would let the other two reintroduce
    # exactly what it excluded.
    allowed = eligible_tags(desc_by_id)
    patterns = _vocab_pattern(allowed)

    # `only` slices the work; the VOTERS are always the whole vault, because a
    # neighbourhood computed from one slice would give a record different tags
    # depending on which chunk it happened to land in.
    wanted = None if only is None else set(only)
    targets = [rid for rid in ids
               if rid in rows and (include_seeded or not is_seeded(rows[rid]))
               and (wanted is None or rid in wanted)]
    if limit is not None:
        targets = targets[:limit]

    changes: list[Change] = []
    for start in range(0, len(targets), BLOCK):
        chunk = targets[start:start + BLOCK]
        block = mat[[index[r] for r in chunk]]
        sims = block @ mat.T                       # (chunk, all records)
        for local, rid in enumerate(chunk):
            changes.append(_plan_one(
                rid, sims[local], ids, index, desc_by_id, tags_by_id,
                rules, patterns, allowed, vault, prune))
    return [c for c in changes if c.is_change]


def _plan_one(rid, sim_row, ids, index, desc_by_id, tags_by_id, rules,
              patterns, allowed, vault, prune) -> Change:
    before = tags_by_id[rid]
    own = list(desc_by_id[rid])

    # --- signal 1: what the neighbourhood is called ---------------------------
    sim = sim_row.copy()
    sim[index[rid]] = -1.0                          # never vote for yourself
    top = np.argpartition(-sim, min(NEIGHBOURS, len(sim) - 1))[:NEIGHBOURS]
    weights: dict[str, float] = {}
    total = 0.0
    for j in top:
        s = float(sim[j])
        if s < NEIGHBOUR_FLOOR:
            continue
        total += s
        for tag in desc_by_id.get(ids[j], ()):
            weights[tag] = weights.get(tag, 0.0) + s
    support = {t: w / total for t, w in weights.items()} if total > 0 else {}

    scored: dict[str, float] = {t: s for t, s in support.items()
                                if s >= PROPAGATE_SHARE and t in allowed}

    # --- signal 2: tags this record's own tags imply --------------------------
    for tag in own:
        for implied, conf in rules.get(tag, {}).items():
            if implied in allowed:
                scored[implied] = max(scored.get(implied, 0.0), conf)

    # --- signal 3: known tags said out loud in the text -----------------------
    # Costs one record decryption, so it runs last and only for records whose
    # text can still change the answer.
    text = None
    spoken: set[str] = set()
    if patterns or (prune and own):
        row = vault.db.get_row(rid)
        if row is not None:
            text = vault.db.decrypt_text(row, vault._master)
            for tag, pat in patterns.items():
                if pat.search(text):
                    spoken.add(tag)
                    scored[tag] = max(scored.get(tag, 0.0), 1.0)
            # A record's OWN tags are checked against its text too, and
            # deliberately without the eligibility filter. Eligibility decides
            # what may be COPIED onto other memories; it has no business
            # deciding whether a memory keeps a tag its own text says out loud.
            # Conflating the two pruned exactly the tags most worth keeping:
            # the rare, specific ones a person chose on purpose.
            if prune:
                for tag, pat in _vocab_pattern(set(own) - spoken).items():
                    if pat.search(text):
                        spoken.add(tag)

    keep = []
    for tag in own:
        if prune and tag not in spoken and support.get(tag, 0.0) < PRUNE_SHARE:
            continue                                # unsupported and unspoken
        keep.append(tag)

    proposed = sorted(set(scored) - set(keep), key=lambda t: -scored[t])
    after_desc = keep + proposed
    after_desc = after_desc[:MAX_TAGS_PER_RECORD]

    protected = [t for t in before if t.startswith(Store.PROTECTED_TAG_PREFIXES)]
    after = protected + after_desc
    return Change(record_id=rid, before=before, after=after,
                  added=[t for t in after if t not in before],
                  removed=[t for t in before if t not in after])


def apply(vault, changes: list[Change], caller: str = "retag") -> int:
    """Write a plan. Returns how many records changed.

    Every write goes through Store.set_tags, which re-adds structural tags
    whatever it is handed, so even a corrupted plan cannot strip a seeded
    record's identity."""
    n = 0
    for c in changes:
        if not c.is_change:
            continue
        vault.db.set_tags(c.record_id, [t for t in c.after
                                        if not t.startswith(Store.PROTECTED_TAG_PREFIXES)])
        n += 1
    if n:
        vault._audit_and_capture(caller, "retag", f"{n} records retagged")
    return n


def run(vault, *, include_seeded: bool = False, prune: bool = False,
        caller: str = "retag") -> dict:
    """Plan and apply in one call. What the background pass and the CLI share."""
    changes = plan(vault, include_seeded=include_seeded, prune=prune)
    n = apply(vault, changes, caller=caller)
    if n:
        vault.save()
    return {"records_changed": n,
            "tags_added": sum(len(c.added) for c in changes),
            "tags_removed": sum(len(c.removed) for c in changes),
            "pruned": prune}
