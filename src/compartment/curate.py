"""Vault curation passes: atomize (split blob memories) and the opinions audit.

Both repair a vault that predates the store gate and the opinion kind. They
are deliberately deterministic about MECHANICS and delegate all JUDGEMENT:

* `atomize` never invents the split. Compartment never calls an LLM and never
  touches the network, so the intelligence that turns one 2,000-character
  blob into five one-claim memories comes from outside - the user's own
  agent, reading the listing this module produces and writing the plan this
  module applies. What is guaranteed here is the bookkeeping the comment in
  Vault.store always promised: every piece keeps the blob's original
  `created`, `discovered` and `source`, so the migration that restates a
  memory more atomically never destroys the one date it exists to preserve.
  The blob is then superseded, not deleted: history stays readable by id.

* The opinions audit finds what the update-first store path cannot: pairs of
  live opinions on one subject stored before the kind existed. It backfills
  `kind` on records whose text is opinion-shaped, clusters live opinions by
  embedding similarity, and either reports the clusters for a human or agent
  to reconcile, or applies the one resolution that needs no judgement:
  keep-newest, superseding the older stance in each cluster.
"""
from __future__ import annotations

import json
import re

import numpy as np

from . import gate
from .crypto import CryptoError
from .vault import (OPINION_UPDATE_THRESHOLD, Vault, is_seeded, local_stamp,
                    strip_provenance)


def _limit(v: Vault, max_chars: int | None) -> int:
    if max_chars is not None:
        return int(max_chars)
    return int(v.config.settings.get("max_memory_chars",
                                     gate.DEFAULT_MAX_CHARS))


def _organic_rows(v: Vault, where: str = ""):
    sql = ("SELECT id FROM records WHERE pack IS NULL "
           "AND superseded_by IS NULL " + where + " ORDER BY created, id")
    for r in v.db.conn.execute(sql):
        row = v.db.get_row(r["id"])
        if row is None or is_seeded(row["tags"]):
            continue
        yield row


# --------------------------------------------------------------- atomize

def oversized(v: Vault, caller: str = "user",
              max_chars: int | None = None) -> list[dict]:
    """Every organic live record whose claim exceeds the gate limit, oldest
    first, with everything a splitting agent needs to write the plan.

    Audited like every other path that hands memory text to a reader."""
    limit = _limit(v, max_chars)
    out = []
    for row in _organic_rows(v):
        text = v.db.decrypt_text(row, v._master)
        claim = strip_provenance(text)
        if len(claim) <= limit:
            continue
        out.append({
            "id": row["id"], "chars": len(claim), "text": text,
            "namespace": row["ns"], "tags": json.loads(row["tags"]),
            "importance": row["importance"], "kind": row["kind"] or "fact",
            "created": row["created"],
            "created_local": local_stamp(row["created"]),
            "source": row["source"], "discovered": row["discovered"],
        })
    v._audit_and_capture(caller, "curate-list",
                         f"atomize listing, {len(out)} over-limit records")
    return out


def parse_plan(text: str) -> list[dict]:
    """Plan JSONL: one line per blob, {"id": ..., "pieces": [{"text": ...,
    optional "tags", "importance", "kind"}, ...]}.

    Split on newlines only, never on splitlines(): json.dumps with
    ensure_ascii=False leaves U+2028 and U+2029 unescaped inside strings,
    and splitlines() would cut a valid plan line in half at them."""
    plan = []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CryptoError(f"plan line {n}: not JSON ({exc})") from None
        if not isinstance(entry, dict) or not entry.get("id"):
            raise CryptoError(f"plan line {n}: needs an 'id'")
        pieces = entry.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            raise CryptoError(f"plan line {n}: needs a non-empty 'pieces' list")
        plan.append(entry)
    return plan


def apply_plan(v: Vault, plan: list[dict], caller: str = "user",
               max_chars: int | None = None) -> dict:
    """Store each blob's pieces with the blob's own dates, then supersede it.

    The WHOLE plan is validated before anything is written: a bad line
    refused halfway through would leave some blobs replaced and some not,
    with nothing to say which. Fixing an error must not introduce one."""
    limit = _limit(v, max_chars)
    seen_ids: dict[str, int] = {}
    for n, entry in enumerate(plan, 1):
        if entry["id"] in seen_ids:
            raise CryptoError(f"plan line {n}: {entry['id']} already appears "
                              f"on line {seen_ids[entry['id']]} - one entry "
                              f"per blob")
        seen_ids[entry["id"]] = n
        row = v.db.get_row(entry["id"])
        if row is None:
            raise CryptoError(f"plan line {n}: no record {entry['id']!r}")
        if row["pack"] is not None or is_seeded(row["tags"]):
            raise CryptoError(f"plan line {n}: {entry['id']} is shipped "
                              f"content, not an organic memory")
        if row["superseded_by"]:
            raise CryptoError(f"plan line {n}: {entry['id']} is already "
                              f"superseded by {row['superseded_by']}")
        for m, piece in enumerate(entry["pieces"], 1):
            text = (piece.get("text") or "").strip() \
                if isinstance(piece, dict) else ""
            if not text:
                raise CryptoError(f"plan line {n} piece {m}: no text")
            reason = gate.rejection(text, limit)
            if reason:
                raise CryptoError(f"plan line {n} piece {m}: {reason}")
            kind = (piece.get("kind") or "fact")
            if str(kind).strip().lower() not in ("fact", "opinion"):
                raise CryptoError(f"plan line {n} piece {m}: kind={kind!r} "
                                  f"is not 'fact' or 'opinion'")
            imp = piece.get("importance")
            if imp is not None:
                try:
                    float(imp)
                except (TypeError, ValueError):
                    raise CryptoError(
                        f"plan line {n} piece {m}: importance={imp!r} is "
                        f"not a number") from None
            tags = piece.get("tags")
            if tags is not None and not (isinstance(tags, list) and
                                         all(isinstance(t, str)
                                             for t in tags)):
                raise CryptoError(f"plan line {n} piece {m}: tags must be "
                                  f"a list of strings")

    blobs = pieces_stored = dupes = 0
    for entry in plan:
        old = v.db.get_row(entry["id"])
        old_tags = [t for t in json.loads(old["tags"])
                    if not t.startswith("id:")]
        anchor, anchor_dup = None, False
        for piece in entry["pieces"]:
            def _store(dedup: bool):
                return v.store(
                    piece["text"].strip(), caller=caller,
                    namespace=old["ns"],
                    tags=piece.get("tags") or old_tags,
                    importance=float(piece.get("importance")
                                     if piece.get("importance") is not None
                                     else old["importance"]),
                    source=old["source"], discovered=old["discovered"],
                    created=old["created"],
                    kind=piece.get("kind") or "fact",
                    # Shape was validated above; _gate=False also keeps the
                    # opinion bands out of a restore of old material, whose
                    # conflicts the opinions audit reconciles afterwards.
                    _gate=False, _dedup=dedup)
            res = _store(True)
            if res.get("duplicate") and res["id"] == entry["id"]:
                # A condensed piece can score >= 0.97 against the very blob
                # it condenses. That match says nothing about the rest of
                # the vault, and the blob is about to be retired - so store
                # the piece for real rather than anchoring the blob to
                # itself.
                res = _store(False)
            if res.get("duplicate"):
                dupes += 1
            else:
                pieces_stored += 1
            # The anchor prefers a freshly stored piece; a piece that
            # deduplicated against another live record still qualifies,
            # because that record is where the claim now lives.
            if anchor is None or (res.get("duplicate") is False
                                  and anchor_dup):
                anchor, anchor_dup = res["id"], bool(res.get("duplicate"))
        v.supersede(entry["id"], anchor, caller=caller)
        blobs += 1
    return {"blobs_superseded": blobs, "pieces_stored": pieces_stored,
            "duplicate_pieces_skipped": dupes}


# --------------------------------------------------------- opinions audit

#: Text shapes that mark a stored claim as a stance rather than a fact.
#: Conservative on purpose: mislabelling a fact as an opinion subjects it to
#: the update-first store path and the fast-decay recency prior, so a miss
#: here is cheaper than a false hit. The audit reports every reclassification
#: it makes, and `kind` is a plain column - any single record is put back
#: with one UPDATE.
_OPINION_HINT = re.compile(
    r"\b(?:prefers?|preferred|preference|likes|dislikes?|loves|hates?|"
    r"wants|advised|advise[sd]?\s+against|recommend(?:s|ed|ation)?|"
    r"decided\s+(?:to|against|that)|policy|stance|opinion|believes?|"
    r"feels\s+that|rule\s+of\s+thumb)\b", re.IGNORECASE)


def backfill_kinds(v: Vault, caller: str = "user") -> list[dict]:
    """Mark opinion-shaped unclassified records kind='opinion'. Returns what
    changed; the caller's save() is what persists it."""
    changed = []
    for row in _organic_rows(v, "AND kind IS NULL"):
        claim = strip_provenance(v.db.decrypt_text(row, v._master))
        if _OPINION_HINT.search(claim):
            v.db.set_kind(row["id"], "opinion")
            changed.append({"id": row["id"], "text": claim})
    if changed:
        v._audit_and_capture(caller, "retag-kind",
                             f"{len(changed)} records marked opinion")
    return changed


def opinion_clusters(v: Vault, threshold: float | None = None,
                     caller: str = "user") -> list[list[dict]]:
    """Groups of live opinions similar enough to be one subject, each group
    newest-first. These are exactly the records the update-first store path
    would have refused to twin, had they arrived after it existed.

    Clustered WITHIN each namespace, never across: the store path's own
    conflict check is namespace-scoped, two agents' namespaces are two
    scopes on purpose, and a cross-namespace cluster would invite
    keep_newest to retire a record in favour of one its caller may not
    even be allowed to read."""
    thr = float(threshold if threshold is not None else
                v.config.settings.get("opinion_update_threshold",
                                      OPINION_UPDATE_THRESHOLD))
    by_ns: dict[str, list] = {}
    for row in _organic_rows(v, "AND kind = 'opinion'"):
        by_ns.setdefault(row["ns"], []).append(row)
    out = []
    for rows in by_ns.values():
        if len(rows) < 2:
            continue
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32)
                         for r in rows])
        # Vectorized pair-finding: the naive Python double loop walked
        # eighteen million pairs on a six-thousand-opinion vault. numpy
        # finds the above-threshold pairs; union-find only walks those.
        pairs = np.argwhere(np.triu(mat @ mat.T >= thr, k=1))
        parent = list(range(len(rows)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, j in pairs:
            parent[find(int(i))] = find(int(j))
        groups: dict[int, list] = {}
        for i, row in enumerate(rows):
            groups.setdefault(find(i), []).append(row)
        for members in groups.values():
            if len(members) < 2:
                continue
            entries = []
            for row in members:
                ref = row["affirmed"] or row["created"]
                e = {"id": row["id"], "namespace": row["ns"], "ref": ref,
                     "text": v.db.decrypt_text(row, v._master),
                     "created_local": local_stamp(row["created"])}
                if row["affirmed"]:
                    e["affirmed_local"] = local_stamp(row["affirmed"])
                entries.append(e)
            entries.sort(key=lambda e: -e["ref"])
            out.append(entries)
    out.sort(key=len, reverse=True)
    n_records = sum(len(c) for c in out)
    v._audit_and_capture(caller, "curate-list",
                         f"opinion clusters, {len(out)} groups over "
                         f"{n_records} records")
    return out


def keep_newest(v: Vault, clusters: list[list[dict]],
                caller: str = "user") -> list[dict]:
    """The judgement-free resolution: in each cluster the most recently
    affirmed stance survives and supersedes the rest. Anything subtler - a
    merge, a deliberate coexistence - is done by hand or by an agent through
    the ordinary store/supersede surface.

    Every supersede in every cluster is validated before the first one is
    applied, so a grant refusal on cluster three cannot land after cluster
    one's decisions are already journaled."""
    for cluster in clusters:
        newest = cluster[0]
        v.config.grant_for(caller, v.db.get_row(newest["id"])["ns"])
        for older in cluster[1:]:
            v._supersede_check(older["id"], caller)
    actions = []
    for cluster in clusters:
        newest = cluster[0]
        for older in cluster[1:]:
            v.supersede(older["id"], newest["id"], caller=caller)
            actions.append({"superseded": older["id"], "by": newest["id"]})
    return actions
