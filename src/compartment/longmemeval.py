"""LongMemEval retrieval harness (`compartment bench --longmemeval`).

Measures Compartment's retrieval pipeline on LongMemEval (Wu et al., ICLR 2025):
500 questions, each against tens of multi-turn chat sessions of history.
For every question we embed every history turn with the bundled model and
score with the SAME fusion Compartment uses in production - ranking.evidence(),
a weighted soft OR in log space over a vector channel and a literal-evidence
channel, with a reciprocal-rank residue - then aggregate turns to sessions by
their best turn and check whether the evidence sessions surface at the top.

Fidelity to the product is the whole point, so the harness mirrors the vault
side for side rather than approximating it. Specifically it must keep doing
all of this, because each one was a real divergence that moved the number:
  - turns are embedded as OVERLAPPING WINDOWS (embed_record), not one
    truncated 512-token vector per turn, and a turn scores by its best window;
  - the keyword channel tries implicit AND first and only then ORs the terms
    whose document frequency is under COMMON_TERM_FRACTION, exactly as
    Store.fts_search does;
  - document frequency is measured by FTS5 token match, not by substring
    containment, so it agrees with the weights it produces;
  - literal evidence is computed ONLY for keyword hits, bounded by
    LEX_COVERAGE_DEPTH, and vector-only candidates score 0.0 for it, as in
    Vault._rank_candidates.
The timed region covers retrieval only. Building the per-question keyword
index is setup, and the vault does not pay it per query, so it sits outside
the timer: with it inside, the reported latency was mostly index construction
over thousands of turns and was not comparable to anything.

Reported numbers (compare with what other memory systems advertise):
- Recall@Any@k - at least one evidence session in the top k
- Recall@All@k - every evidence session in the top k
Abstention questions (id suffix "_abs") carry no evidence and are skipped,
as in the benchmark's own retrieval evaluation.

Pure tooling: this never touches a vault and adds zero runtime cost to the
product. The dataset is fetched once via `compartment setup download-longmemeval`
- like download-model, an explicit user-invoked network operation; the
benchmark run itself is fully offline.
"""
from __future__ import annotations

from .home import env, home
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import math

import numpy as np

from .crypto import CryptoError
from .embed import DEFAULT_MODEL, Embedder

# The benchmark scores the PRODUCT, so it imports the product's ranking rather
# than restating it. A benchmark with its own copy of the formula measures the
# copy, and the number stops meaning anything the moment the two drift.
from .ranking import (CANDIDATE_POOL, COMMON_TERM_FRACTION, LEX_COVERAGE_DEPTH,
                      evidence, information_coverage, p_from_cosine)
# Same reason: the query is split into terms by the store's own tokenizer, and
# percentiles come from the same nearest-rank helper the other bench reports
# use. `int(len(xs) * p)` overshoots by one rank and published a maximum as a
# p95 for any run of twenty questions or fewer.
from .bench import _pct
from .store import Store

VARIANTS = {
    "s": "longmemeval_s",
    "m": "longmemeval_m",
    "oracle": "longmemeval_oracle",
}
BASE_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/"


def data_dir() -> Path:
    return home() / "benchmarks" / "longmemeval"


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

def _build_fts(texts: list[str]) -> sqlite3.Connection:
    """The per-question keyword index. Setup, not retrieval: the vault keeps a
    persistent FTS index and never rebuilds one per query, so this is built
    before the timer starts and closed by the caller."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE fts USING fts5(text)")
    con.executemany("INSERT INTO fts (rowid, text) VALUES (?, ?)",
                    list(enumerate(texts)))
    return con


def _match(con: sqlite3.Connection, expr: str, limit: int) -> list[int]:
    if not expr:
        return []
    try:
        rows = con.execute(
            "SELECT rowid, rank FROM fts WHERE fts MATCH ? ORDER BY rank "
            "LIMIT ?", (expr, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [int(r[0]) for r in rows]


def _doc_frequency(con: sqlite3.Connection, term: str) -> int:
    """How many turns contain this term, by FTS5 token match - the same
    measure Store.doc_frequency uses. Substring containment counted a
    different thing from the weights it fed."""
    try:
        return con.execute(
            "SELECT count(*) c FROM fts WHERE fts MATCH ?",
            ('"' + term.replace('"', "") + '"',)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _fts_ranks(con: sqlite3.Connection, query: str, limit: int,
               total: int) -> dict[int, int]:
    """Mirror of Store.fts_search: implicit AND first, and only if that finds
    nothing, OR the terms that carry information (dropping any appearing in
    more than COMMON_TERM_FRACTION of the corpus)."""
    terms = Store._terms(query)
    if not terms:
        return {}
    hits = _match(con, " ".join(f'"{t}"' for t in terms), limit)
    if not hits:
        ceiling = max(1, int(total * COMMON_TERM_FRACTION))
        keep = [t for t in terms
                if _doc_frequency(con, t) <= ceiling] or terms
        hits = _match(con, " OR ".join(f'"{t}"' for t in keep), limit)
    return {ti: rank for rank, ti in enumerate(hits)}


def _term_information(con: sqlite3.Connection, query: str,
                      total: int) -> dict[str, float]:
    """Mirror of Store.term_information: log(N/(1+df)) in nats, per term."""
    info = {}
    for t in Store._terms(query):
        df = _doc_frequency(con, t)
        if df > 0:
            info[t] = math.log(total / (1.0 + df))
    return info


def _score_question(inst: dict, embedder: Embedder,
                    cache: dict[str, np.ndarray], ks: tuple[int, ...]) -> dict:
    sessions = inst["haystack_sessions"]
    session_ids = inst["haystack_session_ids"]
    # NOT `evidence`: that is the name of the imported scoring function, and
    # binding it here made it a local for the whole body, so the call below
    # reached the set instead and raised "'set' object is not callable" on
    # every scored question - after the entire embedding pass had run.
    answer_sessions = set(inst["answer_session_ids"])

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
        for t in dict.fromkeys(missing):
            cache[t] = embedder.embed_record(t)

    # One row per WINDOW, mapped back to its turn, exactly as the vault indexes
    # windows and maps them back to records.
    win_turn: list[int] = []
    for ti, t in enumerate(turn_texts):
        win_turn.extend([ti] * len(cache[t]))
    mat = np.vstack([cache[t] for t in turn_texts])

    n_turns = max(1, len(turn_texts))
    con = _build_fts(turn_texts)          # setup, deliberately before t0
    try:
        t0 = time.perf_counter()
        qvec = embedder.embed_query(inst["question"])
        sims = mat @ qvec

        # Best window per turn, first-appearance rank, pool of DISTINCT turns:
        # Vault._rank_candidates does exactly this over windows and records.
        vec_score: dict[int, float] = {}
        v_rank: dict[int, int] = {}
        for wi in np.argsort(-sims)[:CANDIDATE_POOL * 4]:
            ti = win_turn[int(wi)]
            s = float(sims[int(wi)])
            if s > vec_score.get(ti, -2.0):
                vec_score[ti] = s
            if ti not in v_rank:
                v_rank[ti] = len(v_rank)
            if len(vec_score) >= CANDIDATE_POOL:
                break

        fts = _fts_ranks(con, inst["question"], CANDIDATE_POOL, n_turns)
        info = _term_information(con, inst["question"], n_turns)

        # Literal evidence for KEYWORD HITS ONLY, best-first and bounded, and
        # 0.0 for vector-only candidates. Giving every candidate a coverage
        # score handed literal evidence to turns the product scores at zero.
        lex_p: dict[int, float] = {}
        if sum(info.values()) > 0 and fts:
            for ti in sorted(fts, key=fts.get)[:LEX_COVERAGE_DEPTH]:
                lex_p[ti] = information_coverage(info, turn_texts[ti])

        fused: dict[int, float] = {}
        for ti in set(vec_score) | set(fts):
            fused[ti] = evidence(p_from_cosine(vec_score.get(ti)),
                                 lex_p.get(ti, 0.0),
                                 v_rank.get(ti), fts.get(ti))

        sess_score: dict[int, float] = {}
        for ti, sc in fused.items():
            si = turn_sess[ti]
            sess_score[si] = max(sess_score.get(si, 0.0), sc)
        ranked = [session_ids[si]
                  for si in sorted(sess_score, key=sess_score.get, reverse=True)]
        ms = (time.perf_counter() - t0) * 1000
    finally:
        con.close()

    out = {"skipped": False, "ms": ms, "type": inst.get("question_type", "?")}
    for k in ks:
        top = set(ranked[:k])
        out[f"any@{k}"] = bool(answer_sessions & top)
        out[f"all@{k}"] = answer_sessions <= top
    return out


def run(variant: str = "s", limit: int | None = None,
        ks: tuple[int, ...] = (5, 10)) -> dict:
    p = dataset_path(variant)
    if not p.exists():
        raise CryptoError(
            f"LongMemEval dataset not found at {p}. Fetch it once with: "
            f"compartment setup download-longmemeval --variant {variant}")
    instances = json.loads(p.read_text(encoding="utf-8"))
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

    # Turns are embedded as windows now, so a turn maps to (n_windows, dim)
    # rather than one vector. The file name carries that shape: an older
    # flat ".vecs-" cache cannot be read as this and must not be tried.
    cache_file = dataset_path(variant).with_suffix(
        f".winvecs-{embedder.model_sha256[:12]}.npz")
    legacy_file = dataset_path(variant).with_suffix(
        f".vecs-{embedder.model_sha256[:12]}.npz")
    disk: dict[str, np.ndarray] = {}
    if cache_file.exists():
        # Close the NpzFile before _flush_cache() later os.replace()s over it:
        # np.load keeps the archive open, and Windows refuses to replace a file
        # with a live handle. Member arrays are materialized on access, so the
        # dict is fully built inside the with-block.
        with np.load(cache_file) as z:
            hashes, vecs, offs = z["hashes"], z["vecs"], z["offsets"]
            disk = {h: vecs[offs[i]:offs[i + 1]]
                    for i, h in enumerate(hashes)}
        print(f"  loaded {len(disk)} cached turn vectors from "
              f"{cache_file.name}")
    texts_all = list(uniq)
    if not disk and legacy_file.exists():
        # A single-window turn embeds identically either way, and that is the
        # overwhelming majority of them, so a pre-window cache is still worth
        # most of its value. Reuse exactly the entries whose text still chunks
        # to one window and re-embed the rest, rather than throwing away an
        # hour of work on a rename.
        with np.load(legacy_file) as z:
            old = dict(zip(z["hashes"], z["vecs"]))
        kept = 0
        for t in texts_all:
            v = old.get(_h(t))
            if v is not None and len(embedder.chunk(t)) == 1:
                disk[_h(t)] = np.atleast_2d(v)
                kept += 1
        print(f"  reused {kept} of {len(old)} vectors from the pre-window "
              f"cache {legacy_file.name}")
    for t in texts_all:
        v = disk.get(_h(t))
        if v is not None:
            cache[t] = v
    texts = sorted((t for t in texts_all if t not in cache), key=len)
    if texts:
        def _flush_cache() -> None:
            tmp = cache_file.with_suffix(".tmp.npz")
            keys = list(disk.keys())
            counts = [len(disk[k]) for k in keys]
            offsets = np.zeros(len(keys) + 1, dtype=np.int64)
            np.cumsum(counts, out=offsets[1:])
            np.savez_compressed(
                tmp,
                hashes=np.array(keys),
                offsets=offsets,
                vecs=(np.vstack([disk[k] for k in keys]).astype(np.float32)
                      if keys else np.zeros((0, embedder.dim), np.float32)))
            tmp.replace(cache_file)

        print(f"  embedding {len(texts)} unique turns (bundled model, "
              "offline)…")
        t_emb = time.time()
        since_flush = 0
        for i in range(0, len(texts), 256):
            chunk = texts[i:i + 256]
            # Window every turn in the batch, embed all the windows in one
            # pass, then hand each turn back its own rows.
            windows = [embedder.chunk(t) for t in chunk]
            flat = [w for ws in windows for w in ws]
            vecs = embedder.embed_passages(flat, batch=256)
            at = 0
            for t, ws in zip(chunk, windows):
                cache[t] = vecs[at:at + len(ws)]
                disk[_h(t)] = cache[t]
                at += len(ws)
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
        if not rows:
            return 0.0
        return round(100.0 * sum(r[key] for r in rows) / len(rows), 1)

    out: dict = {
        "benchmark": "LongMemEval retrieval",
        "variant": VARIANTS[variant],
        "model": DEFAULT_MODEL,
        "questions_scored": len(scored),
        "abstention_skipped": skipped_abs,
        # What the scorer actually is. This used to name the additive scorer
        # that was deleted from this file, constants and all.
        "fusion": "ranking.evidence(): weighted soft OR in log space over a "
                  "vector channel and a literal-evidence channel, plus a "
                  "reciprocal-rank residue; turn = best window, "
                  "session = best turn",
        # Turns carry no importance and no stored timestamp, so the
        # multiplicative prior final_score() applies has nothing to read here
        # and is deliberately not part of this measurement.
        "prior_applied": False,
    }
    if not scored:
        # Every question was an abstention or had no usable turns. Report that
        # honestly instead of dying on a divide-by-zero after doing all the
        # embedding work.
        out["note"] = ("no questions were scored - every instance was an "
                       "abstention question or had no usable turns; "
                       "recall and latency are undefined")
        for k in ks:
            out[f"recall_any@{k}"] = None
            out[f"recall_all@{k}"] = None
        out["by_type"] = {}
        out["query_ms_p50"] = None
        out["query_ms_p95"] = None
        out["unique_turns_embedded"] = len(cache)
        return out
    for k in ks:
        out[f"recall_any@{k}"] = pct(scored, f"any@{k}")
        out[f"recall_all@{k}"] = pct(scored, f"all@{k}")
    out["by_type"] = {
        t: {f"recall_all@{ks[0]}": pct(rows, f"all@{ks[0]}"),
            "questions": len(rows)}
        for t, rows in sorted(by_type.items())}
    out["query_ms_p50"] = round(_pct(lat, 0.50), 1)
    out["query_ms_p95"] = round(_pct(lat, 0.95), 1)
    out["unique_turns_embedded"] = len(cache)
    return out
