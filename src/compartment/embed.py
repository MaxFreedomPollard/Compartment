"""Local embeddings via the BUNDLED int8 ONNX model. Zero network, ever.

The default model ships inside the package as package data; its SHA-256 is
pinned here and verified at load (fail-fast on any mismatch). Optional models
live in the user model directory and carry their own pinned hashes recorded
at download time by `compartment setup download-model`.
"""
from __future__ import annotations

from .home import env, home
import dataclasses
import functools
import json
import os
import threading
from pathlib import Path

import numpy as np

from .crypto import CryptoError, sha256

DEFAULT_MODEL = "bge-small-en-v1.5-int8"
DEFAULT_DIM = 384

# BGE is trained asymmetrically: passages are embedded bare, queries are
# embedded behind this instruction. Leaving it off costs retrieval accuracy on
# exactly the short-query-to-long-passage case a memory vault is made of. It
# applies to the QUERY side only, so turning it on changes no stored vector and
# no existing vault needs rebuilding.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

# The model reads 512 tokens and no more. Text past that is not "weighted less"
# by the encoder, it is not seen at all, so a long memory used to be searchable
# only by its opening. Records are therefore embedded as OVERLAPPING WINDOWS
# and scored by their best window.
#
# The window is 448 rather than 512 to leave room for [CLS]/[SEP] and for a
# query instruction, and the stride is 384 so consecutive windows share 64
# tokens - a fact that straddles a boundary still sits whole inside one of
# them. MAX_CHUNKS caps a pathological record at ~24k tokens of vectors;
# beyond that the tail stays keyword-searchable, which is the honest tradeoff
# rather than an unbounded index.
CHUNK_WINDOW = 448
CHUNK_STRIDE = 384
MAX_CHUNKS = 64

# Padded tokens per inference batch. The attention tensors inside the encoder
# scale with batch x sequence^2, so a batch is not "64 texts", it is however
# many texts fit under this many padded tokens: 64 short memories of about 60
# tokens, or 9 full 448-token windows. Measured on an M1 with the int8 model, a
# batch of 64 full windows peaked at 1.3-1.5 GB of RAM whatever the allocator
# did afterwards; 8 windows peaked at 250 MB and took the same time overall
# (3.5 s for 64 windows either way). Texts are sorted by length before
# packing, so a batch never pads sixty short texts out to the length of one
# long one.
BATCH_TOKENS = 4096
# Pinned hashes of the bundled model files (recorded at bundling time).
BUNDLED_HASHES = {
    "model_quantized.onnx": "6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}

# Optional models fetchable by `compartment setup download-model` (the ONLY
# network-capable code path in the product lives in cli.py setup).
OPTIONAL_MODELS = {
    "bge-small-en-v1.5-fp32": {
        "dim": 384,
        "files": {
            "model.onnx": "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model.onnx",
            "tokenizer.json": "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/tokenizer.json",
        },
    },
    "multilingual-e5-small-int8": {
        "dim": 384,
        "prefix_query": "query: ",
        "prefix_passage": "passage: ",
        "files": {
            "model_quantized.onnx": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx",
            "tokenizer.json": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/tokenizer.json",
        },
    },
}


class ModelError(CryptoError):
    pass


def bundled_model_dir() -> Path:
    return Path(__file__).resolve().parent / "models" / DEFAULT_MODEL


def user_model_dir() -> Path:
    return Path(env("MODEL_DIR", home() / "models"))


def resolve_model_dir(name: str) -> Path:
    if name == DEFAULT_MODEL:
        return bundled_model_dir()
    d = user_model_dir() / name
    if not d.is_dir():
        raise ModelError(
            f"Model {name!r} is not installed. Run: compartment setup download-model {name}"
        )
    return d


def _verify_hashes(d: Path, expected: dict[str, str], label: str) -> None:
    for fname, want in expected.items():
        p = d / fname
        if not p.is_file():
            raise ModelError(f"{label}: missing file {fname}")
        got = sha256(p.read_bytes())
        if got != want:
            raise ModelError(
                f"{label}: SHA-256 mismatch for {fname} "
                f"(expected {want[:16]}…, got {got[:16]}…). Refusing to load."
            )


@dataclasses.dataclass(frozen=True)
class ModelInfo:
    """Everything about a model that can be known without loading it."""
    name: str
    dir: Path
    onnx: Path
    tokenizer: Path
    sha256: str
    dim: int
    prefix_query: str
    prefix_passage: str


@functools.lru_cache(maxsize=None)
def model_info(model_name: str = DEFAULT_MODEL) -> ModelInfo:
    """Verify a model's files against their pins and describe it. No runtime.

    Opening a vault checks that the model on this machine is the one the vault
    was built with, and it used to do that by constructing an Embedder: an
    ONNX session and a tokenizer, about 50 MB of RAM and a fifth of a second,
    in every process that so much as reads a record - the menu bar app's
    status poll, `compartment status`, every MCP server at startup whether or
    not the agent ever searches. The pin is a SHA-256 of a file, and hashing
    the file is all the check ever needed. The result is cached per process;
    the files do not change underneath a running install, and when they do
    (an upgrade replaced the tree) a stale answer here is no worse than the
    session that was already running on the old files.
    """
    d = resolve_model_dir(model_name)
    if model_name == DEFAULT_MODEL:
        _verify_hashes(d, BUNDLED_HASHES, "bundled model")
        return ModelInfo(model_name, d, d / "model_quantized.onnx",
                         d / "tokenizer.json",
                         BUNDLED_HASHES["model_quantized.onnx"], DEFAULT_DIM,
                         BGE_QUERY_INSTRUCTION, "")
    pin_file = d / "HASHES.json"
    if not pin_file.is_file():
        raise ModelError(f"model {model_name}: HASHES.json missing (re-download)")
    pins = json.loads(pin_file.read_text(encoding="utf-8"))
    _verify_hashes(d, pins["files"], f"model {model_name}")
    onnx_file = next(p for p in d.glob("*.onnx"))
    # Verified equal to the file a moment ago, so the pin IS the file's hash.
    sha = pins["files"].get(onnx_file.name) or sha256(onnx_file.read_bytes())
    return ModelInfo(model_name, d, onnx_file, d / "tokenizer.json", sha,
                     int(pins["dim"]), pins.get("prefix_query", ""),
                     pins.get("prefix_passage", ""))


def _missing_runtime_error(exc: ImportError) -> ModelError | None:
    """The actionable form of onnxruntime's Windows DLL failure, or None.

    A clean Windows install has no Microsoft Visual C++ runtime, and
    onnxruntime will not import without it: `DLL load failed while importing
    onnxruntime_pybind11_state`. CI never sees this because GitHub's runners
    ship the runtime preinstalled; a fresh Windows 11 VM reproduced it on the
    first `compartment init`. The bare ImportError names a module, not the
    remedy, so someone hitting it has no way to know one download fixes it."""
    if os.name == "nt" and "DLL load failed" in str(exc):
        return ModelError(
            "onnxruntime cannot load: this Windows machine is missing the "
            "Microsoft Visual C++ runtime it is built against. Install "
            "https://aka.ms/vs/17/release/vc_redist.x64.exe (one small "
            "installer, no restart needed), then run this command again.")
    return None


class Embedder:
    """CLS-pooled, L2-normalized sentence embeddings from a local ONNX model."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        try:
            import onnxruntime as ort      # local import: keeps CLI startup fast
        except ImportError as exc:
            better = _missing_runtime_error(exc)
            if better is not None:
                raise better from exc
            raise
        from tokenizers import Tokenizer

        # Errors only, and for the DEFAULT logger, not just the session's:
        # device discovery runs under the default logger and its warnings go
        # to stderr - on Azure/Hyper-V it complains about the PCI paths every
        # time, on every CLI call that embeds.
        try:
            ort.set_default_logger_severity(3)
        except Exception:                                   # noqa: BLE001
            pass

        info = model_info(model_name)
        self.info = info
        self.model_name = model_name
        self.dim = info.dim
        self.prefix_query = info.prefix_query
        self.prefix_passage = info.prefix_passage
        self.model_sha256 = info.sha256
        self.tok = Tokenizer.from_file(str(info.tokenizer))
        self.tok.enable_truncation(max_length=512)
        # Padding is done here, per batch, in _infer. The tokenizer's own
        # padding pads a batch out to its longest member, which is exactly the
        # waste the length-sorted packing below exists to avoid.
        self.tok.no_padding()
        # The tokenizer is one mutable object: chunk() switches truncation off
        # and back on around its call. Inference runs concurrently just fine,
        # so the lock covers the tokenizer only, not the session.
        self._tok_lock = threading.Lock()
        so = ort.SessionOptions()
        so.log_severity_level = 3
        # No memory arena. onnxruntime's arena keeps every byte it ever grew
        # to and grows again on the next batch: measured, one batch of 64
        # long windows took a process from 118 MB to 1.5 GB resident, a
        # second identical batch to 3 GB, and neither was ever given back.
        # Six MCP servers on one laptop each did this on their own. Without
        # the arena the same work returns to under 90 MB when it is done, at
        # the same speed for the batch sizes BATCH_TOKENS allows.
        so.enable_cpu_mem_arena = False
        self.sess = ort.InferenceSession(str(info.onnx), so,
                                         providers=["CPUExecutionProvider"])
        self._needs_token_type = any(
            i.name == "token_type_ids" for i in self.sess.get_inputs())

    def _encode(self, texts: list[str]) -> list[list[int]]:
        """Token ids per text, truncated to the model's 512, never padded."""
        with self._tok_lock:
            return [e.ids for e in self.tok.encode_batch(texts)]

    def _infer(self, ids: list[list[int]]) -> np.ndarray:
        """CLS-pool one padded batch. Pads with id 0 and mask 0, which is what
        the tokenizer's own padding produced, so nothing about a vector
        depends on which batch its text happened to land in."""
        width = max(len(x) for x in ids)
        arr = np.zeros((len(ids), width), dtype=np.int64)
        mask = np.zeros((len(ids), width), dtype=np.int64)
        for row, x in enumerate(ids):
            arr[row, :len(x)] = x
            mask[row, :len(x)] = 1
        feed = {"input_ids": arr, "attention_mask": mask}
        if self._needs_token_type:
            feed["token_type_ids"] = np.zeros_like(arr)
        out = self.sess.run(None, feed)[0]
        cls = out[:, 0].astype(np.float32)
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cls / norms

    def _run(self, texts: list[str]) -> np.ndarray:
        return self._infer(self._encode(texts))

    @staticmethod
    def _batches(order: list[int], ids: list[list[int]], batch: int,
                 budget: int = BATCH_TOKENS) -> list[list[int]]:
        """Group length-sorted indices so that no batch exceeds `batch` texts
        or `budget` padded tokens. The last index added to a group is its
        longest, so the padded size is the group's length times that."""
        groups: list[list[int]] = []
        current: list[int] = []
        for i in order:
            if current and (len(current) >= batch
                            or (len(current) + 1) * len(ids[i]) > budget):
                groups.append(current)
                current = []
            current.append(i)
        if current:
            groups.append(current)
        return groups

    def embed_passages(self, texts: list[str], batch: int = 64) -> np.ndarray:
        texts = [self.prefix_passage + t for t in texts]
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        ids = self._encode(texts)
        order = sorted(range(len(ids)), key=lambda i: len(ids[i]))
        out = np.empty((len(ids), self.dim), dtype=np.float32)
        for group in self._batches(order, ids, batch):
            out[group] = self._infer([ids[i] for i in group])
        return out

    def embed_query(self, text: str) -> np.ndarray:
        return self._run([self.prefix_query + text])[0]

    def chunk(self, text: str, window: int = CHUNK_WINDOW,
              stride: int = CHUNK_STRIDE, max_chunks: int = MAX_CHUNKS) -> list[str]:
        """Split text into overlapping windows measured in MODEL tokens.

        Measured in tokens, not characters: a character budget is a guess that
        is wrong by a factor of three between prose and a hex digest, and being
        wrong here means silently dropping the tail of a memory. The tokenizer
        already knows the answer, and its offsets map every window back to a
        clean character span so no chunk starts mid-word.
        """
        if not text:
            return [text]
        with self._tok_lock:
            self.tok.no_truncation()
            try:
                enc = self.tok.encode(text, add_special_tokens=False)
            finally:
                self.tok.enable_truncation(max_length=512)
        ids = enc.ids
        if len(ids) <= window:
            return [text]
        offs = enc.offsets
        out: list[str] = []
        start = 0
        while start < len(ids) and len(out) < max_chunks:
            end = min(start + window, len(ids))
            out.append(text[offs[start][0]:offs[end - 1][1]])
            if end >= len(ids):
                break
            start += stride
        return out

    def embed_record(self, text: str) -> np.ndarray:
        """(n_chunks, dim) for one record. Row 0 is always its opening."""
        return self.embed_passages(self.chunk(text))


def get_embedder(model_name: str = DEFAULT_MODEL, *, shared: bool | None = None):
    """The embedder a vault should use: the machine's shared embedding
    process when it is available, this process's own model otherwise.

    `shared=None` means "whatever the environment says", which is on
    everywhere the daemon runs (see embed_daemon.enabled). The two objects
    answer the same calls with the same vectors, so callers hold whichever
    they are given and never ask which. The fallback is inside the remote
    object too: if the daemon goes away mid-session the vault keeps working
    on a local model without anyone asking it to.
    """
    from . import embed_daemon      # local import: the daemon imports us
    if embed_daemon.enabled(shared):
        remote = embed_daemon.RemoteEmbedder.connect(model_name)
        if remote is not None:
            return remote
    return Embedder(model_name)
