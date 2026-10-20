"""The brute-force index keeps its rows in place as it grows.

Every vault below 20,000 records searches a BruteForceIndex, and every store
adds one row to it. The rows lived in one matrix that each add replaced with
np.vstack, a copy of every row the index held: about 2 ms per store at
20,000 rows, and quadratic on the paths that add thousands at once (init's
6,664 starting memories, an import). An add now writes one row into spare
capacity. What must not change is what search reads: the same rows, in the
same order, under the same keys.

The vectors are small integers, so every dot product is exact whatever order
a BLAS library sums it in, and comparing scores compares the rows each index
holds rather than the machine's arithmetic. Accelerate, for one, sums a
matrix that does not start on a 64-byte boundary in another order.
"""
import numpy as np

from compartment.vindex import BruteForceIndex

DIM = 384


class VstackIndex:
    """The index as it was: one matrix, replaced whole by every add."""

    def __init__(self, dim):
        self._keys = []
        self._mat = np.zeros((0, dim), dtype=np.float32)

    def add(self, ikey, vec):
        self._keys.append(ikey)
        self._mat = np.vstack([self._mat, vec.astype(np.float32)[None, :]])

    def remove(self, ikey):
        try:
            i = self._keys.index(ikey)
        except ValueError:
            return
        self._keys.pop(i)
        self._mat = np.delete(self._mat, i, axis=0)

    search = BruteForceIndex.search


def _vectors(rng, n, dim=DIM):
    return rng.integers(-8, 9, size=(n, dim)).astype(np.float32)


def _same(idx, ref, queries):
    """The same keys and rows, in the same order, and the same answers."""
    assert idx._keys == ref._keys
    assert idx._mat.shape == ref._mat.shape
    assert np.array_equal(idx._mat, ref._mat)
    for q in queries:
        for k in (1, 10, len(ref._keys) + 5):
            assert idx.search(q, k) == ref.search(q, k)


def test_rows_added_one_at_a_time_are_the_rows_vstack_built():
    rng = np.random.default_rng(1)
    queries = _vectors(rng, 3)
    inputs = list(_vectors(rng, 3000))
    for i in range(0, 3000, 3):
        inputs[i] = inputs[i].astype(np.float64)    # stored as float32 too
    idx = BruteForceIndex(DIM)
    checkpoints = {0, 1, 15, 16, 17, 31, 32, 33, 1023, 1024, 1025, 2047,
                   2048, 2049, 2999}
    for i, vec in enumerate(inputs):
        idx.add(1000 + i, vec)
        if i in checkpoints:
            ref = VstackIndex(DIM)
            ref._keys = [1000 + j for j in range(i + 1)]
            ref._mat = np.vstack([v.astype(np.float32)[None, :]
                                  for v in inputs[:i + 1]])
            _same(idx, ref, queries)


def test_adds_and_removes_in_any_order_leave_the_same_rows():
    """What forget, expire and supersede do between stores: remove a key
    from anywhere, or one that is not there (a windowed record's own ikey
    never is), then go on adding."""
    dim = 32
    rng = np.random.default_rng(2)
    queries = _vectors(rng, 2, dim)
    idx, ref = BruteForceIndex(dim), VstackIndex(dim)
    live, nxt = [], 0
    for step in range(1500):
        op = rng.random()
        if op < 0.55 or not live:
            vec = _vectors(rng, 1, dim)[0]
            idx.add(nxt, vec)
            ref.add(nxt, vec)
            live.append(nxt)
            nxt += 1
        elif op < 0.9:
            key = live.pop(int(rng.integers(len(live))))
            idx.remove(key)
            ref.remove(key)
        else:
            idx.remove(10 ** 9 + step)
            ref.remove(10 ** 9 + step)
        _same(idx, ref, queries)
    for key in live:                                   # empty it, then refill
        idx.remove(key)
        ref.remove(key)
    _same(idx, ref, queries)
    assert len(idx) == 0 and idx.search(queries[0], 5) == []
    for key in range(5):
        vec = _vectors(rng, 1, dim)[0]
        idx.add(key, vec)
        ref.add(key, vec)
    _same(idx, ref, queries)


def test_an_index_built_at_once_keeps_its_rows_as_it_grows():
    """An unlock builds the index from every vector at once; the stores that
    follow add past that size, and the buffer grows under them. A removal
    right after a growth and an add right after a removal are the edges."""
    dim = 32
    rng = np.random.default_rng(3)
    queries = _vectors(rng, 2, dim)
    base = _vectors(rng, 100, dim)
    given = base.copy()
    idx = BruteForceIndex.build(dim, list(range(100)), base)
    ref = VstackIndex(dim)
    ref._keys, ref._mat = list(range(100)), base.copy()
    nxt = 100
    for _ in range(3):
        for _ in range(len(ref._keys) + 1):            # past the next growth
            vec = _vectors(rng, 1, dim)[0]
            idx.add(nxt, vec)
            ref.add(nxt, vec)
            nxt += 1
        _same(idx, ref, queries)
        first = ref._keys[0]
        for key in (nxt - 1, first):
            idx.remove(key)
            ref.remove(key)
        _same(idx, ref, queries)
        vec = _vectors(rng, 1, dim)[0]
        idx.add(nxt, vec)
        ref.add(nxt, vec)
        nxt += 1
        _same(idx, ref, queries)
    assert np.array_equal(base, given), "build() must not write into its input"


def test_an_add_writes_one_row_instead_of_copying_the_index():
    """The cost itself, counted rather than timed: across 3,000 adds the
    rows already held move only when the buffer doubles, where np.vstack
    moved every one of them on every add."""
    rng = np.random.default_rng(4)
    idx = BruteForceIndex(DIM)
    moves, before = 0, idx._mat
    for i, vec in enumerate(_vectors(rng, 3000)):
        idx.add(i, vec)
        if not np.shares_memory(before, idx._mat):
            moves += 1
        before = idx._mat
    assert moves <= 12, f"the rows moved on {moves} of 3,000 adds"

    built = BruteForceIndex.build(DIM, list(range(1000)), _vectors(rng, 1000))
    moves, before = 0, built._mat
    for i, vec in enumerate(_vectors(rng, 1000)):
        built.add(1000 + i, vec)
        if not np.shares_memory(before, built._mat):
            moves += 1
        before = built._mat
    assert moves <= 2, f"the rows moved on {moves} of 1,000 adds"


def test_a_mistyped_precision_setting_still_builds_an_index(capsys):
    """index_precision is hand-edited JSON, and usearch raised KeyError for
    anything it does not spell. Above the brute-force limit the index is
    built on every open, so one typo meant the vault would not open."""
    import pytest
    pytest.importorskip("usearch")
    from compartment.vindex import build_index
    rng = np.random.default_rng(3)
    mat = rng.standard_normal((50, 16)).astype(np.float32)
    for setting in ("int-8", 8, None, "b1", ["f32"]):
        idx = build_index(16, list(range(1, 51)), mat, precision=setting,
                          force="hnsw")
        assert idx.kind == "hnsw/usearch (f32)" and len(idx) == 50
        assert len(idx.search(mat[7], 3)) == 3      # built, and it searches
    assert "index_precision" in capsys.readouterr().err
    for spelled, kind in (("INT8", "i8"), (" i8", "i8"), ("float16", "f16")):
        assert build_index(16, list(range(1, 51)), mat, precision=spelled,
                           force="hnsw").kind == f"hnsw/usearch ({kind})"


def test_a_large_vault_is_not_given_an_hnsw_index_usearch_would_build_serially(
        monkeypatch):
    """Without SIMD - which is what usearch reports inside macOS Seatbelt,
    where it cannot ask the kernel what the CPU supports - the graph is
    built at a few hundred vectors a second, on every open: minutes for a
    vault past the limit, against milliseconds per exact search."""
    import pytest
    pytest.importorskip("usearch")
    from usearch import index as usearch_index

    from compartment import vindex

    built = []

    class _Serial(usearch_index.Index):
        hardware_acceleration = "serial"

        def add(self, *args, **kwargs):
            built.append(args)
            return super().add(*args, **kwargs)

    monkeypatch.setattr(usearch_index, "Index", _Serial)
    monkeypatch.setattr(vindex, "BRUTE_FORCE_LIMIT", 10)
    rng = np.random.default_rng(5)
    mat = rng.standard_normal((40, 16)).astype(np.float32)
    idx = vindex.build_index(16, list(range(1, 41)), mat)
    assert isinstance(idx, BruteForceIndex)
    assert not built, "no graph was built only to be thrown away"
    assert idx.search(mat[3], 1)[0][0] == 4

    forced = vindex.build_index(16, list(range(1, 41)), mat, force="hnsw")
    assert forced.kind.startswith("hnsw"), "an explicit force is honoured"


def test_with_simd_a_large_vault_still_gets_hnsw(monkeypatch):
    import pytest
    pytest.importorskip("usearch")
    from usearch import index as usearch_index

    from compartment import vindex

    class _Simd(usearch_index.Index):
        hardware_acceleration = "neon"

    monkeypatch.setattr(usearch_index, "Index", _Simd)
    monkeypatch.setattr(vindex, "BRUTE_FORCE_LIMIT", 10)
    mat = np.random.default_rng(6).standard_normal((40, 16)).astype(np.float32)
    idx = vindex.build_index(16, list(range(1, 41)), mat)
    assert idx.kind.startswith("hnsw")
    assert idx.search(mat[3], 1)[0][0] == 4

