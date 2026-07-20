"""Post-install health check: canned queries against the frozen core
starting memories (ids core-001..core-260, seeded into "main" at init).

Each query must place its expected record in the top 3 hybrid-search hits.
Doubles as the relevance regression benchmark (the core facts never change
within a major version).
"""
from __future__ import annotations

import json
import time

# (query, expected core-facts record id)
QUERIES = [
    ("what is the capital of France", "core-001"),
    ("capital city of Japan", "core-030"),
    ("which element has atomic number 79", "core-087"),
    ("chemical symbol for iron", "core-081"),
    ("how many centimeters in an inch", "core-093"),
    ("what temperature does water boil at", "core-107"),
    ("when did World War II end", "core-122"),
    ("year of the first moon landing", "core-126"),
    ("when did the Berlin Wall fall", "core-125"),
    ("how fast does light travel", "core-151"),
    ("how many bones are in the human body", "core-159"),
    ("what does DNA stand for", "core-166"),
    ("which gas makes up most of Earth's atmosphere", "core-176"),
    ("deepest point in the ocean", "core-195"),
    ("tallest mountain on Earth", "core-194"),
    ("largest hot desert in the world", "core-196"),
    ("what is the value of pi", "core-221"),
    ("square root of 144", "core-222"),
    ("largest planet in the solar system", "core-246"),
    ("who was the first person to walk on the moon", "core-257"),
]

TOP_N = 3


def run(vault, caller: str = "selftest") -> dict:
    """Returns {passed, failed, total, latencies_ms, failures}."""
    idmap = _seed_id_map(vault)
    if not idmap:
        return {"passed": 0, "failed": len(QUERIES), "total": len(QUERIES),
                "error": "no starting memories found (run `engram init`)",
                "failures": ["starting memories missing"], "latencies_ms": []}
    latencies, failures = [], []
    for query, want in QUERIES:
        t0 = time.perf_counter()
        res = vault.search(query, caller=caller, top_k=TOP_N)
        ms = (time.perf_counter() - t0) * 1000
        latencies.append(round(ms, 1))
        got = [idmap.get(r["id"]) for r in res["results"]]
        if want not in got:
            failures.append(f"{query!r}: expected {want}, got {got}")
    passed = len(QUERIES) - len(failures)
    return {"passed": passed, "failed": len(failures), "total": len(QUERIES),
            "latencies_ms": latencies,
            "p50_ms": sorted(latencies)[len(latencies) // 2],
            "max_ms": max(latencies),
            "failures": failures}


def _seed_id_map(vault) -> dict[str, str]:
    """Map vault record id → original seed id, via the stable "id:" tag the
    seeding path preserves. Reads the (plaintext-in-RAM) tags column
    directly - no per-record decrypt, no audit noise."""
    out = {}
    for row in vault.db.conn.execute(
            "SELECT id, tags FROM records WHERE tags LIKE '%id:%'"):
        for t in json.loads(row["tags"]):
            if t.startswith("id:"):
                out[row["id"]] = t[3:]
    return out
