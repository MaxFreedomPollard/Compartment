"""Hash-chained, tamper-evident audit log (stored inside the sealed payload).

Each entry's hash covers the previous entry's hash, so any edit, deletion,
or reordering of history breaks the chain at a detectable point.
A failure to write the audit entry fails the operation (fail-fast), never
the other way around.
"""
from __future__ import annotations

import hashlib
import json
import time

GENESIS = "GENESIS"


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
    """Walk the chain. Returns (ok, entries_checked, message)."""
    prev = GENESIS
    n = 0
    for row in conn.execute("SELECT * FROM audit ORDER BY seq"):
        if row["prev_hash"] != prev:
            return False, n, f"chain break at seq {row['seq']}: prev_hash mismatch"
        want = _entry_hash(prev, row["ts"], row["caller"], row["op"], row["detail"])
        if row["hash"] != want:
            return False, n, f"chain break at seq {row['seq']}: entry hash mismatch"
        prev = row["hash"]
        n += 1
    return True, n, f"audit chain intact ({n} entries)"


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
    return changed, first
