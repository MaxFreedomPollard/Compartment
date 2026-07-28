"""The zero-network proof, the frozen relevance regression, and perf budgets."""
import socket
import urllib.request

import pytest

from compartment import bench, offline_guard, selftest
from compartment.vindex import BruteForceIndex, UsearchIndex, build_index

import numpy as np


@pytest.fixture()
def offline():
    offline_guard.activate()
    yield
    offline_guard.deactivate()


def test_guard_blocks_network(offline):
    with pytest.raises(offline_guard.OfflineViolation):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(Exception):
        urllib.request.urlopen("http://example.com", timeout=2)


def test_full_lifecycle_with_sockets_blocked(offline, tmp_path):
    """The headline claim: init → seed → selftest → store → search → shred →
    lock → unlock, all with network creation blocked."""
    from compartment import packs
    from compartment.vault import Vault
    from conftest import PASS, seed_pack_bytes

    vp = str(tmp_path / "offline.vault")
    v = Vault.create(vp, PASS)
    packs.seed_records(v, seed_pack_bytes(), caller="offline-test")
    st = selftest.run(v)
    assert st["failed"] == 0, st["failures"]
    r = v.store("offline lifecycle memory", caller="offline-test")
    assert v.search("lifecycle", caller="offline-test")["results"]
    v.forget(r["id"], caller="offline-test", shred=True)
    v.lock()
    v2 = Vault.unlock(vp, passphrase=PASS)
    assert v2.db.count() == 6665


def test_seed_relevance_regression(seeded_vault):
    """Frozen benchmark: all 20 canned queries must hit top-3."""
    st = selftest.run(seeded_vault)
    assert st["total"] == 20
    assert st["failed"] == 0, st["failures"]
    assert st["max_ms"] < 500  # generous CI bound; laptop p50 is ~2ms


def test_index_parity_brute_vs_hnsw():
    rng = np.random.default_rng(7)
    mat = rng.standard_normal((3000, 64)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    keys = list(range(1, 3001))
    bf = BruteForceIndex.build(64, keys, mat)
    hnsw = UsearchIndex.build(64, keys, mat)
    q = mat[1234]
    top_bf = [k for k, _ in bf.search(q, 5)]
    top_h = [k for k, _ in hnsw.search(q, 5)]
    assert top_bf[0] == top_h[0] == 1235  # exact self-match survives HNSW
    assert len(set(top_bf) & set(top_h)) >= 4  # ≥80% overlap @5


def test_index_add_remove():
    idx = build_index(8, [], np.zeros((0, 8), np.float32))
    v = np.ones(8, np.float32) / np.sqrt(8)
    idx.add(42, v)
    assert idx.search(v, 1)[0][0] == 42
    idx.remove(42)
    assert idx.search(v, 1) == []


def test_bench_budgets(seeded_vault):
    out = bench.run(seeded_vault, synthetic_n=5000, queries=20)
    assert out["budgets"]["vector_search_p95_under_100ms"], out
    # None means the platform cannot measure RSS at all (no `resource`
    # module, ie. Windows). It must never read as a pass, and it must not be
    # asserted as one either.
    rss = out["budgets"]["rss_under_1gb"]
    if out["peak_rss_mb"] is None:
        assert rss is None, out
    else:
        assert rss, out


# --------------------------------------------------------------- bench fixes

def _ns_count(vault, ns="bench"):
    return vault.db.conn.execute(
        "SELECT COUNT(*) AS n FROM records WHERE ns = ?", (ns,)).fetchone()["n"]


def _audit_ops(vault, op):
    return [r["detail"] for r in vault.db.conn.execute(
        "SELECT detail FROM audit WHERE op = ?", (op,))]


def test_bench_teardown_spares_user_records_in_bench_namespace(vault):
    """A user memory stored under the namespace "bench" must survive a run.

    `compartment store --namespace bench` is a supported thing to do, and the
    old teardown deleted the whole namespace.
    """
    from compartment import audit

    keep = vault.store("the spare key lives under the third flowerpot",
                       caller="user", namespace="bench")
    assert not keep.get("duplicate")

    out = bench.run(vault, synthetic_n=200, queries=5)
    assert out["synthetic_records"] == 200

    # the user's record is untouched, content and all
    assert vault.get(keep["id"], caller="user")["text"] == \
        "the spare key lives under the third flowerpot"
    # and it is the ONLY thing left in the namespace: the 20 bench records went
    assert _ns_count(vault) == 1

    # the deletions went through the audited path, not straight to db.delete
    forgets = [d for d in _audit_ops(vault, "forget")]
    assert len(forgets) == 20, forgets
    assert keep["id"] not in " ".join(forgets)
    ok, _entries, msg = audit.verify(vault.db.conn)
    assert ok, msg


def test_bench_teardown_runs_when_the_run_fails(vault, monkeypatch):
    """A crash after the records are written must still clean them up."""
    def boom(*a, **kw):
        raise RuntimeError("index build exploded")

    monkeypatch.setattr(bench, "build_index", boom)
    with pytest.raises(RuntimeError):
        bench.run(vault, synthetic_n=200, queries=5)
    assert _ns_count(vault) == 0
    assert len(_audit_ops(vault, "forget")) == 20


def test_bench_rejects_zero_records_before_writing_anything(vault):
    from compartment.crypto import CryptoError

    with pytest.raises(CryptoError):
        bench.run(vault, synthetic_n=0)
    with pytest.raises(CryptoError):
        bench.run(vault, synthetic_n=-5)
    with pytest.raises(CryptoError):
        bench.run(vault, synthetic_n=10, queries=0)
    assert _ns_count(vault) == 0
    assert vault.db.count() == 0


def test_bench_reports_rss_as_unmeasured_without_the_resource_module(vault,
                                                                    monkeypatch):
    """No `resource` module means no measurement, so no passing budget."""
    monkeypatch.setattr(bench, "resource", None)
    assert bench._rss_mb() is None
    out = bench.run(vault, synthetic_n=200, queries=5)
    assert out["peak_rss_mb"] is None, out
    assert out["budgets"]["rss_under_1gb"] is None, out
    assert not out["budgets"]["rss_under_1gb"]      # never truthy when unmeasured
    assert "peak_rss_mb_note" in out


def test_pct_is_nearest_rank_not_the_maximum():
    xs = [float(i) for i in range(1, 21)]        # 20 samples, as bench uses
    assert bench._pct(xs, 0.95) == 19.0          # NOT 20.0, the maximum
    assert bench._pct(xs, 0.50) == 10.0
    assert bench._pct(xs, 1.00) == 20.0
    assert bench._pct([7.0], 0.95) == 7.0
    assert bench._pct([float(i) for i in range(1, 101)], 0.95) == 95.0
    assert bench._pct([3.0, 1.0, 2.0], 0.50) == 2.0   # sorts first
    with pytest.raises(ValueError):
        bench._pct([], 0.95)
