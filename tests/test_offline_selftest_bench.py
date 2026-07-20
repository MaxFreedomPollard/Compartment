"""The zero-network proof, the frozen relevance regression, and perf budgets."""
import socket
import urllib.request

import pytest

from engram import bench, offline_guard, selftest
from engram.vindex import BruteForceIndex, UsearchIndex, build_index

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
    from engram import packs
    from engram.vault import Vault
    from conftest import PASS, seed_pack_bytes

    vp = str(tmp_path / "offline.vault")
    v, words = Vault.create(vp, PASS)
    packs.seed_records(v, seed_pack_bytes(), caller="offline-test")
    st = selftest.run(v)
    assert st["failed"] == 0, st["failures"]
    r = v.store("offline lifecycle memory", caller="offline-test")
    assert v.search("lifecycle", caller="offline-test")["results"]
    v.forget(r["id"], caller="offline-test", shred=True)
    v.lock()
    v2 = Vault.unlock(vp, passphrase=PASS)
    assert v2.db.count() == 4808


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
    assert out["budgets"]["rss_under_1gb"], out
