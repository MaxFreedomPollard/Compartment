"""LongMemEval retrieval harness (`engram bench --longmemeval`).

Measures engRAM's retrieval pipeline on LongMemEval (Wu et al., ICLR 2025):
500 questions, each against tens of multi-turn chat sessions of history.
For every question we embed every history turn with the bundled model and
score with the SAME hybrid fusion engRAM uses in production - reciprocal-
rank fusion over exact vector ranks and BM25 keyword ranks, plus the cosine
magnitude term - then aggregate turns to sessions by their best turn and
check whether the evidence sessions surface at the top.

Reported numbers (compare with what other memory systems advertise):
- Recall@Any@k - at least one evidence session in the top k
- Recall@All@k - every evidence session in the top k
Abstention questions (id suffix "_abs") carry no evidence and are skipped,
as in the benchmark's own retrieval evaluation.

Pure tooling: this never touches a vault and adds zero runtime cost to the
product. The dataset is fetched once via `engram setup download-longmemeval`
- like download-model, an explicit user-invoked network operation; the
benchmark run itself is fully offline.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from .crypto import CryptoError
from .embed import DEFAULT_MODEL, Embedder

RRF_K = 60           # identical to vault.search fusion
COSINE_WEIGHT = 0.02
CANDIDATES = 50

VARIANTS = {
    "s": "longmemeval_s",
    "m": "longmemeval_m",
    "oracle": "longmemeval_oracle",
}
BASE_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/"


def data_dir() -> Path:
    return Path.home() / ".engram" / "benchmarks" / "longmemeval"


def dataset_path(variant: str) -> Path:
    return data_dir() / f"{VARIANTS[variant]}.json"


def download(variant: str = "s") -> Path:
    """The explicit network path (like `setup download-model`)."""
    import urllib.request
    if variant not in VARIANTS:
        raise CryptoError(f"unknown variant {variant!r}; options: s, m, oracle")
    dest = dataset_path(variant)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = BASE_URL + VARIANTS[variant]
    print(f"downloading {url}")
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 22)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            print(f"\r  {done // (1 << 20)} MB", end="", flush=True)
    print(f"\n  sha256 {h.hexdigest()}")
    print(f"  → {dest}")
    return dest


# ---------------------------------------------------------------- retrieval

def _fts_ranks(texts: list[str], query: str, limit: int) -> dict[int, int]:
    """BM25 ranks over the turn corpus, engRAM's FTS5 quoting rules."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE fts USING fts5(text)")
    con.executemany("INSERT INTO fts (rowid, text) VALUES (?, ?)",
                    list(enumerate(texts)))
    safe = " ".join('"' + t.replace('"', "") + '"'
                    for t in query.split() if t.replace('"', ""))
    if not safe:
        return {}
    # OR the terms: a memory query rarely matches every word of a question
    safe = safe.replace('" "', '" OR "')
    try:
        rows = con.execute(
            "SELECT rowid, rank FROM fts WHERE fts MATCH ? ORDER BY rank "
            "LIMIT ?", (safe, limit)).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    return {int(r[0]): rank for rank, r in enumerate(rows)}


def _score_question(inst: dict, embedder: Embedder,
                    cache: dict[str, np.ndarray], ks: tuple[int, ...]) -> dict:
    sessions = inst["haystack_sessions"]
    session_ids = inst["haystack_session_ids"]
    evidence = set(inst["answer_session_ids"])

    turn_texts: list[str] = []
    turn_sess: list[int] = []
    for si, sess in enumerate(sessions):
        for turn in sess:
            text = (turn.get("content") or "").strip()
            if text:
                turn_texts.append(text)
                turn_sess.append(si)
    if not turn_texts:
        return {"skipped": True}

    missing = [t for t in turn_texts if t not in cache]
    if missing:
        uniq = list(dict.fromkeys(missing))
        vecs = embedder.embed_passages(uniq, batch=128)
        for t, v in zip(uniq, vecs):
            cache[t] = v
    mat = np.vstack([cache[t] for t in turn_texts])

    t0 = time.perf_counter()
    qvec = embedder.embed_query(inst["question"])
    sims = mat @ qvec
    order = np.argsort(-sims)[:CANDIDATES]
    fts = _fts_ranks(turn_texts, inst["question"], CANDIDATES)

    fused: dict[int, float] = {}
    for rank, ti in enumerate(order):
        ti = int(ti)
        fused[ti] = (fused.get(ti, 0.0) + 1.0 / (RRF_K + rank + 1)
                     + COSINE_WEIGHT * float(sims[ti]))
    for ti, rank in fts.items():
        fused[ti] = fused.get(ti, 0.0) + 1.0 / (RRF_K + rank + 1)

    sess_score: dict[int, float] = {}
    for ti, sc in fused.items():
        si = turn_sess[ti]
        sess_score[si] = max(sess_score.get(si, 0.0), sc)
    ranked = [session_ids[si]
              for si in sorted(sess_score, key=sess_score.get, reverse=True)]
    ms = (time.perf_counter() - t0) * 1000

    out = {"skipped": False, "ms": ms, "type": inst.get("question_type", "?")}
    for k in ks:
        top = set(ranked[:k])
        out[f"any@{k}"] = bool(evidence & top)
        out[f"all@{k}"] = evidence <= top
    return out


def run(variant: str = "s", limit: int | None = None,
        ks: tuple[int, ...] = (5, 10)) -> dict:
    p = dataset_path(variant)
    if not p.exists():
        raise CryptoError(
            f"LongMemEval dataset not found at {p}. Fetch it once with: "
            f"engram setup download-longmemeval --variant {variant}")
    instances = json.loads(p.read_text())
    if limit:
        instances = instances[:limit]

    embedder = Embedder(DEFAULT_MODEL)
    cache: dict[str, np.ndarray] = {}

    # Embed every unique turn up front, length-sorted so each batch pads to
    # near-uniform token counts (order-of-magnitude faster than mixed
    # batches). Vectors are cached on disk beside the dataset - this is
    # PUBLIC benchmark data, not vault content, so the no-plaintext-on-disk
    # invariant is not in play; re-runs go from ~an hour to seconds.
    uniq: dict[str, None] = {}
    for inst in instances:
        if str(inst.get("question_id", "")).endswith("_abs"):
            continue
        for sess in inst["haystack_sessions"]:
            for turn in sess:
                t = (turn.get("content") or "").strip()
                if t:
                    uniq.setdefault(t, None)

    def _h(t: str) -> str:
        return hashlib.sha1(t.encode()).hexdigest()

    cache_file = dataset_path(variant).with_suffix(
        f".vecs-{embedder.model_sha256[:12]}.npz")
    disk: dict[str, np.ndarray] = {}
    if cache_file.exists():
        z = np.load(cache_file)
        disk = dict(zip(z["hashes"], z["vecs"]))
        print(f"  loaded {len(disk)} cached turn vectors from "
              f"{cache_file.name}")
    texts_all = list(uniq)
    for t in texts_all:
        v = disk.get(_h(t))
        if v is not None:
            cache[t] = v
    texts = sorted((t for t in texts_all if t not in cache), key=len)
    if texts:
        def _flush_cache() -> None:
            tmp = cache_file.with_suffix(".tmp.npz")
            np.savez_compressed(
                tmp,
                hashes=np.array(list(disk.keys())),
                vecs=np.stack(list(disk.values())).astype(np.float32))
            tmp.replace(cache_file)

        print(f"  embedding {len(texts)} unique turns (bundled model, "
              "offline)…")
        t_emb = time.time()
        since_flush = 0
        for i in range(0, len(texts), 256):
            chunk = texts[i:i + 256]
            for t, v in zip(chunk, embedder.embed_passages(chunk, batch=256)):
                cache[t] = v
                disk[_h(t)] = cache[t]
            since_flush += len(chunk)
            if since_flush >= 20_000:      # checkpoint: a kill never costs
                _flush_cache()             # more than ~20k turns of work
                since_flush = 0
            if (i // 256) % 20 == 0:
                done = i + len(chunk)
                rate = done / max(time.time() - t_emb, 1e-9)
                print(f"\r  {done}/{len(texts)} turns ({rate:.0f}/s, "
                      f"~{(len(texts) - done) / max(rate, 1):.0f}s left)",
                      end="", flush=True)
        print(f"\r  embedded {len(texts)} turns in "
              f"{time.time() - t_emb:.0f}s" + " " * 30)
        _flush_cache()
        print(f"  cached {len(disk)} turn vectors → {cache_file.name}")

    scored, skipped_abs, lat = [], 0, []
    by_type: dict[str, list[dict]] = {}
    t_start = time.time()
    for i, inst in enumerate(instances):
        if str(inst.get("question_id", "")).endswith("_abs"):
            skipped_abs += 1
            continue
        r = _score_question(inst, embedder, cache, ks)
        if r.get("skipped"):
            continue
        scored.append(r)
        lat.append(r["ms"])
        by_type.setdefault(r["type"], []).append(r)
        if (i + 1) % 25 == 0:
            print(f"\r  {i + 1}/{len(instances)} questions "
                  f"({time.time() - t_start:.0f}s, "
                  f"{len(cache)} unique turns embedded)", end="", flush=True)
    print()

    def pct(rows: list[dict], key: str) -> float:
        return round(100.0 * sum(r[key] for r in rows) / len(rows), 1)

    out: dict = {
        "benchmark": "LongMemEval retrieval",
        "variant": VARIANTS[variant],
        "model": DEFAULT_MODEL,
        "questions_scored": len(scored),
        "abstention_skipped": skipped_abs,
        "fusion": "RRF(exact vector) + RRF(BM25) + 0.02*cosine, "
                  "session = best turn",
    }
    for k in ks:
        out[f"recall_any@{k}"] = pct(scored, f"any@{k}")
        out[f"recall_all@{k}"] = pct(scored, f"all@{k}")
    out["by_type"] = {
        t: {f"recall_all@{ks[0]}": pct(rows, f"all@{ks[0]}"),
            "questions": len(rows)}
        for t, rows in sorted(by_type.items())}
    lat.sort()
    out["query_ms_p50"] = round(lat[len(lat) // 2], 1)
    out["query_ms_p95"] = round(lat[int(len(lat) * 0.95)], 1)
    out["unique_turns_embedded"] = len(cache)
    return out
