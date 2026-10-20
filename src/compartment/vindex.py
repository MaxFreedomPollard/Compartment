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
        # The rows live in a buffer with spare capacity and _mat is its
        # filled part, so an add writes one row. Copying the whole matrix on
        # every add made a store O(n) and a bulk insert O(n^2). The rows and
        # their order are the ones np.vstack produced, so search reads the
        # same matrix it always did.
        self._buf = np.zeros((0, dim), dtype=np.float32)
        self._mat = self._buf

    @classmethod
    def build(cls, dim: int, ikeys: list[int], mat: np.ndarray) -> "BruteForceIndex":
        idx = cls(dim)
        if len(ikeys):
            idx._keys = list(ikeys)
            idx._mat = idx._buf = np.ascontiguousarray(mat, dtype=np.float32)
        return idx

    def add(self, ikey: int, vec: np.ndarray) -> None:
        n = len(self._keys)
        if n == len(self._buf):                  # full: double the capacity
            grown = np.empty((max(2 * n, 16), self.dim), dtype=np.float32)
            grown[:n] = self._mat
            self._buf = grown
        self._buf[n] = vec
        self._keys.append(ikey)
        self._mat = self._buf[:n + 1]

    def remove(self, ikey: int) -> None:
        try:
            i = self._keys.index(ikey)
        except ValueError:
            return
        self._keys.pop(i)
        # np.delete returns a new matrix, which becomes the buffer.
        self._mat = self._buf = np.delete(self._mat, i, axis=0)

    def search(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        if not self._keys:
            return []
        sims = self._mat @ vec.astype(np.float32)
        k = min(k, sims.shape[0])
        # Partition first, sort only the k that survive. A full argsort orders
        # all n vectors to report k of them, which at 20k records is most of
        # the cost of a search spent arranging results nobody will read.
        if k < sims.shape[0]:
            top = np.argpartition(-sims, k - 1)[:k]
            top = top[np.argsort(-sims[top])]
        else:
            top = np.argsort(-sims)
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
        idx.add_all(ikeys, mat)
        return idx

    def add_all(self, ikeys: list[int], mat: np.ndarray) -> None:
        if len(ikeys):
            self._index.add(np.array(ikeys, dtype=np.uint64),
                            np.ascontiguousarray(mat, dtype=np.float32))

    @property
    def serial(self) -> bool:
        """True when usearch found no SIMD kernels it can use on this CPU
        and computes every distance one element at a time. An older usearch
        that does not say is taken to have them, as it always was."""
        return getattr(self._index, "hardware_acceleration", None) == "serial"

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
_PRECISION_WARNED: set[str] = set()

#: What `index_precision` may name: the float widths and int8, each under the
#: spellings usearch itself accepts. The setting lives in a JSON file people
#: edit by hand, and usearch raised KeyError for anything else ("int-8", 8,
#: "INT8 " with a space): above BRUTE_FORCE_LIMIT records the index is built
#: on every open, so one typo meant the vault could not be opened at all.
#: Its bit and unsigned kinds ("b1", "u8") are refused too: under the cosine
#: metric they do not approximate the vectors, they discard them.
PRECISIONS = {
    "f32": "f32", "float32": "f32", "f16": "f16", "float16": "f16",
    "bf16": "bf16", "bfloat16": "bf16", "f64": "f64", "float64": "f64",
    "i8": "i8", "int8": "i8",
}


def _precision(value) -> str:
    """The usearch dtype `value` names, or f32 with a notice once."""
    name = PRECISIONS.get(str(value).strip().lower())
    if name is not None:
        return name
    if repr(value) not in _PRECISION_WARNED:
        import sys
        print(f"notice: index_precision {value!r} is not one of "
              f"{', '.join(sorted(set(PRECISIONS.values())))}; using f32. "
              "Set it with `compartment reindex --int8` or `--f32`.",
              file=sys.stderr)
        _PRECISION_WARNED.add(repr(value))
    return "f32"


def build_index(dim: int, ikeys: list[int], mat: np.ndarray,
                precision: str = "f32", force: str | None = None) -> VectorIndex:
    """Pick the fastest correct index for the corpus size (or force one).

    Above BRUTE_FORCE_LIMIT we prefer SIMD HNSW (usearch). usearch is an
    OPTIONAL dependency, so if it is not installed we fall back to the exact
    brute-force index (still correct, just slower at large scale) and hint
    once at `pip install compartment[hnsw]`."""
    if force == "brute" or (force is None and len(ikeys) < BRUTE_FORCE_LIMIT):
        return BruteForceIndex.build(dim, ikeys, mat)
    try:
        idx = UsearchIndex(dim, _precision(precision))
        # The HNSW graph is built from scratch on every open, and without
        # SIMD usearch builds it at a few hundred vectors a second: measured
        # 50 s for 25,000 vectors and over five minutes for 100,000, per
        # open, where the exact index answers a search over 100,000 in about
        # 9 ms and costs nothing to build. That is not a rare machine: SIMD
        # dispatch asks the kernel what the CPU supports, and inside macOS
        # Seatbelt - where Claude Code and Codex run an agent's commands -
        # that query is refused, so usearch reports "serial" on every Apple
        # Silicon Mac; likewise on CPUs it has no kernels for. A vault past
        # the limit there took minutes to open in every CLI call and every
        # MCP server reopen, long past the clients' tool-call timeouts.
        # Chosen automatically only; an explicit force is honoured.
        if force is None and idx.serial:
            return BruteForceIndex.build(dim, ikeys, mat)
        idx.add_all(ikeys, mat)
        return idx
    except ImportError:
        global _HNSW_WARNED
        if not _HNSW_WARNED:
            import sys
            print(
                f"notice: {len(ikeys)} vectors exceed the {BRUTE_FORCE_LIMIT} "
                "brute-force limit but usearch (HNSW) is not installed; using "
                "exact brute-force search (correct, slower at this scale). For "
                "faster large-corpus search: pip install "
                "compartment[hnsw]", file=sys.stderr)
            _HNSW_WARNED = True
        return BruteForceIndex.build(dim, ikeys, mat)
