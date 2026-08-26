"""Local embeddings via the BUNDLED int8 ONNX model. Zero network, ever.

The default model ships inside the package as package data; its SHA-256 is
pinned here and verified at load (fail-fast on any mismatch). Optional models
live in the user model directory and carry their own pinned hashes recorded
at download time by `compartment setup download-model`.
"""
from __future__ import annotations

from .home import env, home
import json
import os
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

        self.model_name = model_name
        d = resolve_model_dir(model_name)
        if model_name == DEFAULT_MODEL:
            _verify_hashes(d, BUNDLED_HASHES, "bundled model")
            self.dim = DEFAULT_DIM
            self.prefix_query = BGE_QUERY_INSTRUCTION
            self.prefix_passage = ""
            onnx_file = d / "model_quantized.onnx"
        else:
            pin_file = d / "HASHES.json"
            if not pin_file.is_file():
                raise ModelError(f"model {model_name}: HASHES.json missing (re-download)")
            pins = json.loads(pin_file.read_text(encoding="utf-8"))
            _verify_hashes(d, pins["files"], f"model {model_name}")
            self.dim = int(pins["dim"])
            self.prefix_query = pins.get("prefix_query", "")
            self.prefix_passage = pins.get("prefix_passage", "")
            onnx_file = next(p for p in d.glob("*.onnx"))

        self.model_sha256 = sha256(onnx_file.read_bytes())
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=512)
        self.tok.enable_padding()
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(str(onnx_file), so,
                                         providers=["CPUExecutionProvider"])
        self._needs_token_type = any(
            i.name == "token_type_ids" for i in self.sess.get_inputs())

    def _run(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if self._needs_token_type:
            feed["token_type_ids"] = np.zeros_like(ids)
        out = self.sess.run(None, feed)[0]
        cls = out[:, 0].astype(np.float32)
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cls / norms

    def embed_passages(self, texts: list[str], batch: int = 64) -> np.ndarray:
        texts = [self.prefix_passage + t for t in texts]
        chunks = [self._run(texts[i:i + batch]) for i in range(0, len(texts), batch)]
        return np.vstack(chunks) if chunks else np.zeros((0, self.dim), np.float32)

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
        self.tok.no_truncation()
        try:
            enc = self.tok.encode(text, add_special_tokens=False)
        finally:
            self.tok.enable_truncation(max_length=512)
            self.tok.enable_padding()
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
