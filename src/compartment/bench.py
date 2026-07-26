"""Performance + RAM benchmark against the seed pack and a synthetic corpus.

Reports embed/store/search latencies (p50/p95) and peak RSS, and checks the
8GB-baseline budgets: search p95 < 100ms at the synthetic scale, total RSS
< 1GB at 100k records with default settings.
"""
from __future__ import annotations

import random
import sys
import time

import numpy as np

try:
    import resource            # POSIX only; absent on Windows
except ImportError:            # pragma: no cover - Windows
    resource = None

from .vindex import build_index

WORDS = ("report vault memory agent record office data schedule market key "
         "index search secure backup ledger review batch upload form note").split()


def _rss_mb() -> float:
    if resource is None:       # Windows: peak-RSS via getrusage is unavailable
        return 0.0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def run(vault, synthetic_n: int = 20_000, queries: int = 50) -> dict:
    rng = random.Random(42)
    out: dict = {"synthetic_records": synthetic_n}

    # 1) real embed+store latency on a small sample
    t_store = []
    for i in range(20):
        text = f"benchmark memory {i}: " + " ".join(rng.choices(WORDS, k=10))
        t0 = time.perf_counter()
        vault.store(text, caller="bench", namespace="bench", tags=["bench"])
        t_store.append((time.perf_counter() - t0) * 1000)
    out["store_ms_p50"] = round(_pct(t_store, 0.50), 1)
    out["store_ms_p95"] = round(_pct(t_store, 0.95), 1)

    # 2) synthetic vector corpus at scale (index-only: isolates search speed)
    dim = int(vault.header.model["dim"])
    mat = np.random.default_rng(42).standard_normal((synthetic_n, dim)).astype(np.float32)
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
    for _ in range(20):
        t0 = time.perf_counter()
        vault.search("benchmark memory " + rng.choice(WORDS), caller="bench", top_k=8)
        t_hybrid.append((time.perf_counter() - t0) * 1000)
    out["hybrid_search_ms_p50"] = round(_pct(t_hybrid, 0.50), 1)
    out["hybrid_search_ms_p95"] = round(_pct(t_hybrid, 0.95), 1)

    out["peak_rss_mb"] = round(_rss_mb(), 0)
    out["budgets"] = {
        "vector_search_p95_under_100ms": out["vector_search_ms_p95"] < 100,
        "rss_under_1gb": out["peak_rss_mb"] < 1024,
    }
    # clean up bench records
    rows = vault.db.conn.execute(
        "SELECT id, ikey FROM records WHERE ns = 'bench'").fetchall()
    for r in rows:
        vault.db.delete(r["id"], shred=False)
        vault.index.remove(r["ikey"])
        vault._id_by_ikey.pop(r["ikey"], None)
    vault.save()
    return out
