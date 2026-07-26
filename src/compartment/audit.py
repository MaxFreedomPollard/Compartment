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


def append(conn, caller: str, op: str, detail: str) -> str:
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    ts = time.time()
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
