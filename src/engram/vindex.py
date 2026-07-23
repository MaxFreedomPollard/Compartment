"""RAM-resident vector indexes behind a small swappable interface.

- BruteForceIndex: exact SIMD dot-products (numpy/BLAS). Perfect recall,
  sub-millisecond below ~20k records - used automatically at small scale.
- UsearchIndex: HNSW via usearch (SIMD-accelerated), f32 or int8-quantized.

Both are rebuilt in RAM from the vectors stored inside the sealed payload
(invariant I2: no plaintext index file ever exists on disk).
"""
from __future__ import annotations

import numpy as np

BRUTE_FORCE_LIMIT = 20_000


class VectorIndex:
    kind = "abstract"

    def add(self, ikey: int, vec: np.ndarray) -> None: ...
    def remove(self, ikey: int) -> None: ...
    def search(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        """Returns [(ikey, cosine_similarity)] best-first."""
        raise NotImplementedError
    def __len__(self) -> int: ...


class BruteForceIndex(VectorIndex):
    kind = "brute-force (exact)"

    def __init__(self, dim: int):
        self.dim = dim
        self._keys: list[int] = []
        self._mat = np.zeros((0, dim), dtype=np.float32)

    @classmethod
    def build(cls, dim: int, ikeys: list[int], mat: np.ndarray) -> "BruteForceIndex":
        idx = cls(dim)
        if len(ikeys):
            idx._keys = list(ikeys)
            idx._mat = np.ascontiguousarray(mat, dtype=np.float32)
        return idx

    def add(self, ikey: int, vec: np.ndarray) -> None:
        self._keys.append(ikey)
        self._mat = np.vstack([self._mat, vec.astype(np.float32)[None, :]])

    def remove(self, ikey: int) -> None:
        try:
            i = self._keys.index(ikey)
        except ValueError:
            return
        self._keys.pop(i)
        self._mat = np.delete(self._mat, i, axis=0)

    def search(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        if not self._keys:
            return []
        sims = self._mat @ vec.astype(np.float32)
        top = np.argsort(-sims)[:k]
        return [(self._keys[i], float(sims[i])) for i in top]

    def __len__(self) -> int:
        return len(self._keys)


class UsearchIndex(VectorIndex):
    def __init__(self, dim: int, precision: str = "f32"):
        from usearch.index import Index
        self.kind = f"hnsw/usearch ({precision})"
        self.dim = dim
        self.precision = precision
        self._index = Index(ndim=dim, metric="cos", dtype=precision)

    @classmethod
    def build(cls, dim: int, ikeys: list[int], mat: np.ndarray,
              precision: str = "f32") -> "UsearchIndex":
        idx = cls(dim, precision)
        if len(ikeys):
            idx._index.add(np.array(ikeys, dtype=np.uint64),
                           np.ascontiguousarray(mat, dtype=np.float32))
        return idx

    def add(self, ikey: int, vec: np.ndarray) -> None:
        self._index.add(np.uint64(ikey), vec.astype(np.float32))

    def remove(self, ikey: int) -> None:
        self._index.remove(np.uint64(ikey))

    def search(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        m = self._index.search(vec.astype(np.float32), k)
        return [(int(key), 1.0 - float(dist)) for key, dist in zip(m.keys, m.distances)]

    def __len__(self) -> int:
        return len(self._index)


_HNSW_WARNED = False


def build_index(dim: int, ikeys: list[int], mat: np.ndarray,
                precision: str = "f32", force: str | None = None) -> VectorIndex:
    """Pick the fastest correct index for the corpus size (or force one).

    Above BRUTE_FORCE_LIMIT we prefer SIMD HNSW (usearch). usearch is an
    OPTIONAL dependency, so if it is not installed we fall back to the exact
    brute-force index (still correct, just slower at large scale) and hint
    once at `pip install engram-memory-vault[hnsw]`."""
    if force == "brute" or (force is None and len(ikeys) < BRUTE_FORCE_LIMIT):
        return BruteForceIndex.build(dim, ikeys, mat)
    try:
        return UsearchIndex.build(dim, ikeys, mat, precision=precision)
    except ImportError:
        global _HNSW_WARNED
        if not _HNSW_WARNED:
            import sys
            print(
                f"notice: {len(ikeys)} vectors exceed the {BRUTE_FORCE_LIMIT} "
                "brute-force limit but usearch (HNSW) is not installed; using "
                "exact brute-force search (correct, slower at this scale). For "
                "faster large-corpus search: pip install "
                "engram-memory-vault[hnsw]", file=sys.stderr)
            _HNSW_WARNED = True
        return BruteForceIndex.build(dim, ikeys, mat)
