"""What a search costs: one scoring pass, the namespace names read once,
and a recency population that survives a store.

A default search wants up to 400 candidates (four for each of 100 results),
so on any vault of a few hundred memories its first pool of 200 came up
short: that pool was scored in full, a row read per candidate and up to
256 decryptions, and then thrown away when the pool widened. Every search
also grouped every record by namespace to learn which namespaces exist,
and the first search after each store rebuilt the recency population with
a scan and a sort. These tests pin that search answers exactly as it did
while each of those happens once.

The encoder is a stand-in that embeds every query as one fixed vector, and
the memories carry random vectors written straight into the store, so a
vault of hundreds builds in well under a second.
"""
import numpy as np
import pytest

from compartment.ranking import CANDIDATE_POOL, MAX_RESULTS, POOL_EXPANSIONS
from compartment.vault import Vault

from conftest import PASS, statements_run, table_walks

DIM = 384
WORDS = ("router", "lease", "garden", "invoice", "kettle", "bicycle",
         "dentist", "printer", "passport", "ladder", "boiler", "harbour")


class Encoder:
    """Every query is the same unit vector."""
    dim = DIM

    def __init__(self, qvec):
        self.qvec = qvec

    def embed_query(self, text):
        return self.qvec


def _unit(rng, n):
    m = rng.standard_normal((n, DIM)).astype(np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def _vault(path, n, windows=1):
    """n memories in two namespaces, `windows` random vectors each; one in
    twenty mentions zircon, so a query for it has few keyword hits."""
    v = Vault.create(path, PASS, creator="test")
    rng = np.random.default_rng(n * 10 + windows)
    for i in range(n):
        text = (f"Note {i}: the {WORDS[i % 12]} needs the {WORDS[i * 5 % 12]}"
                + (" and some zircon" if i % 20 == 0 else ""))
        v.db.insert(record_id=None, ns="work" if i % 5 == 0 else "main",
                    text=text, vec=_unit(rng, windows),
                    tags=["note", f"t{i % 4}"], importance=0.5,
                    quarantined=False, pack=None,
                    prov={"host": "h", "agent": "test", "session": "-"},
                    master_key=v._master, created=1.7e9 + 60 * i)
    v._rebuild_index()
    v._embedder = Encoder(_unit(rng, 1)[0])
    return v


def test_a_default_search_scores_its_candidates_once(tmp_path, monkeypatch):
    """The first pool of 200 finds 200 memories by vector and 30 by keyword,
    short of the 400 a default search wants, so the pool widens to 800. Only
    the widened pool's scores are used, so only it is scored."""
    v = _vault(str(tmp_path / "v.vault"), 600)
    passes = []
    real = Vault._rank_candidates

    def counted(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        passes.append(len(out[0]))
        return out
    monkeypatch.setattr(Vault, "_rank_candidates", counted)
    assert v.search("zircon", caller="test")["results"]
    assert len(passes) == 1, f"scored {len(passes)} pools: {passes} candidates"


def _widening(real, want):
    """Vault.search's pool loop as it was: score each pool, widen while it
    holds too few candidates, and keep the last pass scored."""
    def rank(self, query, qvec, pool, namespaces=None, hits=None):
        pool = CANDIDATE_POOL
        for _ in range(POOL_EXPANSIONS):
            out = real(self, query, qvec, pool, namespaces)
            if (len(out[0]) >= want * 4
                    or len(out[0]) >= len(self._id_by_ikey)):
                break
            pool *= 4
        return out
    return rank


CASES = [
    ("zircon", {}),
    ("zircon", {"top_k": 5}),
    ("zircon", {"top_k": 150}),
    ("the router and the lease", {}),
    ("quartz feldspar", {}),                  # no keyword hit at all
    ("zircon", {"namespace": "work"}),
    ("router", {"tags": ["t1"]}),
]


@pytest.mark.parametrize("n,windows", [(40, 1), (600, 1), (300, 10)],
                         ids=["every memory in the first pool",
                              "widened once", "widened twice"])
def test_a_search_answers_exactly_as_the_widening_loop_did(
        tmp_path, monkeypatch, n, windows):
    """Ids, order, scores and every other field, for searches with and
    without top_k: the pool is now chosen from the candidate count alone,
    and the pass that is scored is the one the old loop scored last."""
    v = _vault(str(tmp_path / "v.vault"), n, windows)
    real = Vault._rank_candidates
    for query, kwargs in CASES:
        now = v.search(query, caller="test", **kwargs)
        with monkeypatch.context() as m:
            m.setattr(Vault, "_rank_candidates",
                      _widening(real, kwargs.get("top_k") or MAX_RESULTS))
            then = v.search(query, caller="test", **kwargs)
        assert now == then, (query, kwargs)


def test_a_search_reads_the_namespace_names_once_until_a_write(tmp_path):
    """Which namespaces exist came from a GROUP BY over every record, on
    every search, recent() and relations() call."""
    v = _vault(str(tmp_path / "v.vault"), 50)
    with statements_run(v.db.conn) as ran:
        for _ in range(3):
            v.search("zircon", caller="test")
    assert sum("GROUP BY ns" in sql for sql in ran) == 1


def test_the_namespace_list_follows_every_write(tmp_path):
    """A list kept past a write would hide a new namespace from search,
    keep a deleted one, or miss the legacy starter merge, which re-files
    records with direct SQL. It is handed out as a copy, too."""
    v = _vault(str(tmp_path / "v.vault"), 50)

    def counts():
        return {e["namespace"]: e["records"] for e in v.db.namespaces()}
    before = counts()
    rid = v.store("A first memory about the greenhouse", caller="test",
                  namespace="greenhouse", vec=v._embedder.qvec,
                  _gate=False)["id"]
    assert counts() == {**before, "greenhouse": 1}
    hits = v.search("greenhouse", caller="test")["results"]
    assert hits and hits[0]["id"] == rid
    v.forget(rid, caller="test")
    assert counts() == before
    v.store("A starting memory from a legacy layout", caller="test",
            namespace="packs/starter", pack="starter",
            vec=_unit(np.random.default_rng(1), 1), _gate=False, _dedup=False)
    assert counts()["packs/starter"] == 1
    assert v._merge_legacy_starter() == 1
    assert counts() == {**before, "main": before["main"] + 1}
    v.db.namespaces()[0]["records"] = -1
    assert counts() == {**before, "main": before["main"] + 1}


def test_storing_a_newer_memory_keeps_the_recency_population(tmp_path):
    """The population is the sorted times of the vault's own memories. A
    store dropped it, so the first search after every store scanned and
    sorted all of them again; a memory newer than every other belongs at
    the end, which is where it now goes."""
    v = _vault(str(tmp_path / "v.vault"), 50)
    v.db.recency_times()
    v.store("A memory written after all the others", caller="test",
            vec=_unit(np.random.default_rng(2), 1), _gate=False)
    with statements_run(v.db.conn) as ran:
        v.db.recency_times()
    assert table_walks(v.db.conn, ran) == []


def test_a_population_kept_across_stores_is_the_one_a_rebuild_gives(tmp_path):
    """After each kind of store the kept population and its namespace
    subsets equal a fresh rebuild: newer ones in an old and a new namespace,
    a backdated one (which drops it instead), one tied with the newest, an
    opinion re-affirmed later, and records the population never counts:
    pack content, a starting memory and a record born superseded."""
    v = _vault(str(tmp_path / "v.vault"), 50)
    rng = np.random.default_rng(3)
    scopes = (None, {"main"}, {"work"}, {"greenhouse"},
              {"main", "greenhouse"}, set())

    def kept_equals_rebuilt():
        kept = [v.db.recency_times(s).copy() for s in scopes]
        assert not v.db.recency_times().flags.writeable
        v.db._invalidate_recency()
        rebuilt = [v.db.recency_times(s) for s in scopes]
        for a, b in zip(kept, rebuilt):
            assert np.array_equal(a, b)

    def store(text, **kwargs):
        kwargs.setdefault("vec", _unit(rng, 1))
        return v.store(text, caller="test", _gate=False, _dedup=False,
                       **kwargs)
    kept_equals_rebuilt()
    newest = store("Newer than all of them")["created"]
    kept_equals_rebuilt()
    store("The first memory in a new namespace", namespace="greenhouse",
          created=newest + 1)
    kept_equals_rebuilt()
    store("Tied with the newest", namespace="work", created=newest + 1)
    kept_equals_rebuilt()
    store("Written down late, learned long ago", created=1.7e9 + 90)
    kept_equals_rebuilt()
    store("A stance held, and re-affirmed later", kind="opinion",
          created=1.7e9 + 100, affirmed=newest + 5)
    kept_equals_rebuilt()
    store("Pack content", namespace="packs/demo", pack="demo",
          created=newest + 9)
    store("A starting memory", tags=["core", "id:core-00001"],
          created=newest + 9)
    v.db.insert(record_id=None, ns="main", text="History, born superseded",
                vec=_unit(rng, 1), tags=[], importance=0.5,
                quarantined=False, pack=None, prov={},
                master_key=v._master, created=newest + 9,
                superseded_by="nobody")
    kept_equals_rebuilt()
