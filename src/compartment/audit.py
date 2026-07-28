"""Hash-chained, tamper-evident audit log (stored inside the sealed payload).

Each entry's hash covers the previous entry's hash, so an edit or a reordering
in the MIDDLE of the log breaks the chain at a detectable point.

A forward-only chain cannot catch a truncation of its own tail: lop the last
three entries off and what remains still verifies, just shorter. So the head
and the length are anchored outside the chain, in the meta table, every time
the vault is saved. verify() then requires the chain to EXTEND that anchor:
fewer entries than were anchored, or a different hash at the anchored
position, is reported as removal rather than passing as a clean shorter log.

The anchor is written on save and is absent in vaults written by older
builds. A missing anchor is not a failure - it is an unanchored log, reported
as such, and the next save anchors it.

A failure to write the audit entry fails the operation (fail-fast), never
the other way around.
"""
from __future__ import annotations

import hashlib
import json
import time

GENESIS = "GENESIS"

# meta keys holding the anchor. Additive: a vault without them predates
# anchoring and still opens.
ANCHOR_HEAD = "audit_anchor_head"
ANCHOR_COUNT = "audit_anchor_count"
RELINK_LOG = "audit_relink_log"


def _meta_get(conn, k: str) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else None


def _meta_set(conn, k: str, v: str) -> None:
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]


def anchor(conn) -> None:
    """Pin the current head and length. Called on every save."""
    _meta_set(conn, ANCHOR_HEAD, head(conn))
    _meta_set(conn, ANCHOR_COUNT, str(count(conn)))


def read_anchor(conn) -> tuple[str, int] | None:
    """The pinned (head, count), or None for a vault written before anchoring."""
    h = _meta_get(conn, ANCHOR_HEAD)
    c = _meta_get(conn, ANCHOR_COUNT)
    if h is None or c is None:
        return None
    try:
        return h, int(c)
    except ValueError:
        return None


def _entry_hash(prev_hash: str, ts: float, caller: str, op: str, detail: str) -> str:
    body = json.dumps(
        {"prev": prev_hash, "ts": ts, "caller": caller, "op": op, "detail": detail},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()


def append(conn, caller: str, op: str, detail: str, ts: float | None = None) -> str:
    """Add an entry, linked to whatever is currently at the head.

    `ts` is only passed when replaying a journalled operation, so the entry
    keeps the time it actually happened. It is still linked to the head as it
    stands at replay time, never to the head the original writer saw: that
    head may never have reached disk (read operations are audited in RAM and
    persist only on save), and chaining to an entry nobody else has is what
    leaves a dangling link no future verify can resolve."""
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    ts = time.time() if ts is None else ts
    h = _entry_hash(prev, ts, caller, op, detail)
    conn.execute(
        "INSERT INTO audit (ts, caller, op, detail, prev_hash, hash) VALUES (?,?,?,?,?,?)",
        (ts, caller, op, detail, prev, h),
    )
    return h


def head(conn) -> str:
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS


def verify(conn) -> tuple[bool, int, str]:
    """Walk the chain, then check it against the anchor.

    Returns (ok, entries_checked, message). The walk catches edits and
    reorderings; the anchor catches a truncated tail, which the walk alone
    cannot see because a shortened chain is still internally consistent."""
    prev = GENESIS
    n = 0
    hashes: list[str] = []
    for row in conn.execute("SELECT * FROM audit ORDER BY seq"):
        if row["prev_hash"] != prev:
            return False, n, f"chain break at seq {row['seq']}: prev_hash mismatch"
        want = _entry_hash(prev, row["ts"], row["caller"], row["op"], row["detail"])
        if row["hash"] != want:
            return False, n, f"chain break at seq {row['seq']}: entry hash mismatch"
        prev = row["hash"]
        hashes.append(prev)
        n += 1

    anc = read_anchor(conn)
    if anc is None:
        # Every vault is anchored at creation and re-anchored on every save.
        # A missing anchor means the meta rows were removed, which is exactly
        # the move someone makes to hide a truncation, so it is a failure and
        # not a tolerated older shape.
        return False, n, (
            "audit log has no anchor. Every vault is anchored when it is "
            "created and on every save, so an absent anchor means the meta "
            "rows were removed. A truncated log cannot be detected without "
            "it, so this is reported as tampering.")
    a_head, a_count = anc
    if n < a_count:
        return False, n, (
            f"audit log is SHORTER than its anchor: {n} entries present, "
            f"{a_count} anchored at the last save. {a_count - n} entries were "
            "removed from the end. A forward walk cannot see this, which is "
            "why the length is pinned.")
    if a_count == 0:
        pinned_ok = True
    else:
        pinned_ok = len(hashes) >= a_count and hashes[a_count - 1] == a_head
    if not pinned_ok:
        return False, n, (
            f"audit entry {a_count} does not match the anchored head: history "
            "was rewritten at or before the last save, not merely appended to.")
    return True, n, f"audit chain intact ({n} entries, anchored at {a_count})"


def relink(conn) -> tuple[int, int | None]:
    """Re-link entries whose prev_hash points at something not in the log.

    Only for repairing damage done by builds before this one, which replayed a
    journalled entry with the head its original writer saw rather than the head
    the log actually has. The result was a permanent dangling link: verify
    stopped at it and never reported anything after it again.

    What this does NOT do is paper over an edit. Every entry's own hash covers
    its content, so content tampering is caught by the self-hash check and this
    refuses to touch a log that fails it. Only the links are rebuilt, and only
    forward from the first break. Nothing is deleted, reordered or reworded:
    ts, caller, op and detail are exactly what they were.

    Returns (entries relinked, seq of the first break) - (0, None) if intact."""
    rows = conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()
    for row in rows:
        want = _entry_hash(row["prev_hash"], row["ts"], row["caller"], row["op"],
                           row["detail"])
        if row["hash"] != want:
            raise ValueError(
                f"audit entry seq {row['seq']} does not hash to its own content. "
                "That is content tampering, not a dangling link, and relinking "
                "would destroy the evidence. Refusing.")

    # A dangling link is damage worth repairing. A missing entry is not: the
    # self-hash check above passes for a log with rows deleted, because every
    # surviving row still hashes to its own content, and relinking would then
    # rewrite the chain around the hole and report it as intact. The anchor is
    # the only thing that knows how long the log used to be.
    anc = read_anchor(conn)
    if anc is not None and len(rows) < anc[1]:
        raise ValueError(
            f"audit log has {len(rows)} entries but {anc[1]} were anchored at "
            "the last save. Entries were deleted, not merely unlinked, and "
            "relinking would rewrite the chain around the gap and call it "
            "intact. Refusing.")

    prev, first, changed = GENESIS, None, 0
    for row in rows:
        if row["prev_hash"] != prev:
            if first is None:
                first = row["seq"]
        if first is not None:                    # rewrite everything after it
            h = _entry_hash(prev, row["ts"], row["caller"], row["op"], row["detail"])
            conn.execute("UPDATE audit SET prev_hash = ?, hash = ? WHERE seq = ?",
                         (prev, h, row["seq"]))
            changed += 1
            prev = h
        else:
            prev = row["hash"]

    if changed:
        # Leave a permanent, non-erasable record that the chain was rebuilt.
        # It goes in meta rather than in the log itself: appending an entry
        # would change the log's length, and the repair must not look like
        # ordinary activity. Anyone auditing later can see that a repair
        # happened, when, where it started, and what the head was before.
        try:
            prior = json.loads(_meta_get(conn, RELINK_LOG) or "[]")
        except (ValueError, TypeError):
            prior = []
        prior.append({"when": time.time(), "first_break_seq": first,
                      "entries_relinked": changed,
                      "head_before": rows[-1]["hash"], "head_after": prev})
        _meta_set(conn, RELINK_LOG, json.dumps(prior))
    return changed, first


def relink_history(conn) -> list[dict]:
    """Every repair ever performed on this log. Empty is the normal case."""
    try:
        return json.loads(_meta_get(conn, RELINK_LOG) or "[]")
    except (ValueError, TypeError):
        return []
