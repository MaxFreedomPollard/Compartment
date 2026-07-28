"""Performance + RAM benchmark against the live vault and a synthetic corpus.

Measures three things and reports p50/p95 (nearest-rank) for each: embed+store
latency for 20 real records written to the vault, vector-search latency over a
synthetic corpus of `synthetic_n` random unit vectors (default 20,000) that is
indexed only and never stored in the vault, and full hybrid search (embed +
vector + FTS + fuse) on the live vault. It also reports the peak RSS of this
process for the run, which on a seeded vault is dominated by the model and the
index rather than by the synthetic corpus.

Budgets checked: vector search p95 < 100ms at the synthetic scale, and peak RSS
< 1GB. Peak RSS is only measurable where the POSIX `resource` module exists; on
platforms without it both the measurement and its budget are reported as null
rather than as a pass.

The 20 records the run writes are deleted through the normal audited `forget`
path when the run finishes, including when it fails partway through. Records
that were already in the "bench" namespace are left untouched.
"""
from __future__ import annotations

import math
import random
import sys
import time

import numpy as np

try:
    import resource            # POSIX only; absent on Windows
except ImportError:            # pragma: no cover - Windows
    resource = None

from .crypto import CryptoError
from .vindex import build_index

WORDS = ("report vault memory agent record office data schedule market key "
         "index search secure backup ledger review batch upload form note").split()

SAMPLES = 20                   # store / hybrid-search timing samples


def _rss_mb() -> float | None:
    """Peak RSS of this process in MB, or None where it cannot be measured.

    Returning None rather than 0.0 keeps the budget honest: a platform without
    `resource` has no measurement, so it must not report one and must not pass
    a budget on the strength of it.
    """
    if resource is None:       # Windows: peak-RSS via getrusage is unavailable
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def _pct(xs: list[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value at or above rank ceil(p*N).

    `int(len(xs) * p)` overshoots by one rank, so a 20 sample p95 landed on
    index 19, the maximum, and published a worst case as a p95.
    """
    if not xs:
        raise ValueError("percentile of an empty series")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))
    return xs[k]


def _teardown(vault, record_ids: list[str]) -> None:
    """Delete exactly the records this run created, through the audited path.

    Deleting by namespace would take every record a user had stored under the
    namespace "bench", which `compartment store --namespace bench` lets them
    create. Going through `vault.forget` also leaves an audit entry, which a
    direct `vault.db.delete` does not.
    """
    removed = False
    for rid in record_ids:
        try:
            vault.forget(rid, caller="bench", shred=False)
            removed = True
        except CryptoError:
            # already gone, or no longer writable: nothing to clean up
            continue
    if removed:
        vault.save()


def run(vault, synthetic_n: int = 20_000, queries: int = 50) -> dict:
    if synthetic_n < 1:
        raise CryptoError(
            f"bench: --records must be 1 or more, got {synthetic_n}")
    if queries < 1:
        raise CryptoError(f"bench: queries must be 1 or more, got {queries}")

    rng = random.Random(42)
    out: dict = {"synthetic_records": synthetic_n}
    written: list[str] = []
    try:
        # 1) real embed+store latency on a small sample
        t_store = []
        for i in range(SAMPLES):
            text = f"benchmark memory {i}: " + " ".join(rng.choices(WORDS, k=10))
            t0 = time.perf_counter()
            res = vault.store(text, caller="bench", namespace="bench",
                              tags=["bench"])
            t_store.append((time.perf_counter() - t0) * 1000)
            # a deduplicated store returns somebody else's record id; only the
            # ids this run actually created are ours to delete afterwards
            if not res.get("duplicate"):
                written.append(res["id"])
        out["store_ms_p50"] = round(_pct(t_store, 0.50), 1)
        out["store_ms_p95"] = round(_pct(t_store, 0.95), 1)

        # 2) synthetic vector corpus at scale (index-only: isolates search speed)
        dim = int(vault.header.model["dim"])
        mat = np.random.default_rng(42).standard_normal(
            (synthetic_n, dim)).astype(np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True)
        t0 = time.perf_counter()
        idx = build_index(dim, list(range(1, synthetic_n + 1)), mat,
                          precision=vault.config.settings.get("index_precision", "f32"),
                          force=None)
        out["index_build_s"] = round(time.perf_counter() - t0, 2)
        out["index_kind"] = idx.kind

        t_search = []
        for _ in range(queries):
            q = mat[rng.randrange(synthetic_n)]
            t0 = time.perf_counter()
            idx.search(q, 10)
            t_search.append((time.perf_counter() - t0) * 1000)
        out["vector_search_ms_p50"] = round(_pct(t_search, 0.50), 2)
        out["vector_search_ms_p95"] = round(_pct(t_search, 0.95), 2)

        # 3) full hybrid search on the live vault (embed + vector + FTS + fuse)
        t_hybrid = []
        for _ in range(SAMPLES):
            t0 = time.perf_counter()
            vault.search("benchmark memory " + rng.choice(WORDS),
                         caller="bench", top_k=8)
            t_hybrid.append((time.perf_counter() - t0) * 1000)
        out["hybrid_search_ms_p50"] = round(_pct(t_hybrid, 0.50), 1)
        out["hybrid_search_ms_p95"] = round(_pct(t_hybrid, 0.95), 1)

        rss = _rss_mb()
        out["peak_rss_mb"] = None if rss is None else round(rss, 0)
        out["budgets"] = {
            "vector_search_p95_under_100ms": out["vector_search_ms_p95"] < 100,
            # null, not True: an unmeasured budget is not a met budget
            "rss_under_1gb": None if rss is None else rss < 1024,
        }
        if rss is None:
            out["peak_rss_mb_note"] = (
                "not measured: the POSIX resource module is unavailable here")
    finally:
        _teardown(vault, written)
    return out
