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
import re
import sys
import sysconfig
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


#: Every code point the tokenizer cannot take. In a Python str a code point
#: in U+D800..U+DFFF is always a lone surrogate - a pair is one code point
#: above U+FFFF - and it gets there as half of a cut emoji: a JSON "\\ud83d"
#: escape an MCP client may legally send, or a string sliced mid-pair.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def well_formed(text: str) -> str:
    """`text` with each lone surrogate replaced by U+FFFD.

    tokenizers 0.16 and later refuse a str holding one with TypeError
    ("TextInputSequence must be str"), from every embedding call, so a
    store or a search with half an emoji in it failed outright, in the
    shared daemon and in-process alike, while 0.15 let it through. Every
    path into the tokenizer comes through here, so the daemon and this
    process embed such a text identically, and as the replacement
    character any UTF-8 decoder would have put in its place."""
    return _LONE_SURROGATE.sub("\ufffd", text) if text else text


def bundled_model_dir() -> Path:
    return Path(__file__).resolve().parent / "models" / DEFAULT_MODEL


def user_model_dir() -> Path:
    # COMPARTMENT_MODEL_DIR first, and the home only without it: the default
    # was computed even when the variable was set, so with no home directory
    # (HOME unset and no password-database entry) naming the folder did not
    # help, and a downloaded model could not be found.
    explicit = env("MODEL_DIR")
    return Path(explicit) if explicit else home() / "models"


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
            f"https://aka.ms/vs/17/release/vc_redist.{_vc_redist_arch()}.exe "
            "(one small installer, no restart needed), then run this "
            "command again.")
    return None


def _vc_redist_arch() -> str:
    """Which Visual C++ runtime installer this Python needs: the one for
    the architecture the interpreter was BUILT for, not the machine's. On
    Windows on ARM an ARM64 Python needs vc_redist.arm64.exe, and the x64
    installer the message used to name installs a runtime it cannot load;
    an x64 Python running under emulation there needs the x64 one."""
    plat = sysconfig.get_platform()
    if plat == "win-arm64":
        return "arm64"
    if plat == "win32":
        return "x86"
    return "x64"


def _runtime_not_installed(exc: ModuleNotFoundError) -> ModelError | None:
    """The actionable form of onnxruntime or tokenizers simply not being
    importable, or None for any other missing module.

    pip installs both with Compartment wherever it can, but onnxruntime
    publishes no wheel for some platforms (musl/Alpine, 32-bit ARM, the
    BSDs, Android), where an install made with --no-deps or from a
    distribution's packages can lack it. The bare error was a traceback in
    the CLI, and in the MCP server, which passes the model only the text of
    its own errors, "Error executing tool" with no reason at all."""
    if exc.name not in ("onnxruntime", "tokenizers"):
        return None
    return ModelError(
        f"{exc.name} is not installed for this Python ({sys.executable}), "
        "and embedding needs it. Install it with: "
        f"{sys.executable} -m pip install {exc.name}. Where pip has no "
        f"{exc.name} build for this platform ({sysconfig.get_platform()}), "
        "install your system's own package of it, or run Compartment on a "
        "glibc-based Python (a Debian or Ubuntu image rather than Alpine).")


def _cgroup_cpu_limit(root: str = "/sys/fs/cgroup",
                      own: str = "/proc/self/cgroup") -> float | None:
    """The CPUs a cgroup quota grants this process (2.0 for `docker run
    --cpus 2`), or None where no quota applies.

    cgroup v2 keeps "<quota> <period>" in cpu.max, at every level of the
    hierarchy, and the tightest one on the way up from this process's own
    group is the one that binds; inside a container the namespace makes
    that group the root. cgroup v1 keeps the two numbers in separate files
    of the cpu controller's mount."""
    limits: list[float] = []
    rel = None
    try:
        with open(own, encoding="utf-8") as f:
            for line in f:
                if line.startswith("0::"):
                    rel = line[3:].strip()
    except OSError:
        pass
    if rel is not None:
        top = Path(root)
        here = top / rel.lstrip("/")
        while here == top or top in here.parents:
            try:
                quota, period = (here / "cpu.max").read_text(
                    encoding="ascii").split()[:2]
                if quota != "max" and int(period) > 0:
                    limits.append(int(quota) / int(period))
            except (OSError, ValueError):
                pass
            if here == top:
                break
            here = here.parent
    for d in ("cpu", "cpu,cpuacct", "cpuacct,cpu"):
        try:
            quota = int((Path(root) / d / "cpu.cfs_quota_us").read_text(
                encoding="ascii"))
            period = int((Path(root) / d / "cpu.cfs_period_us").read_text(
                encoding="ascii"))
        except (OSError, ValueError):
            continue
        if quota > 0 and period > 0:
            limits.append(quota / period)
        break
    return min(limits) if limits else None


def intra_op_threads() -> int:
    """Threads for one inference, or 0 for onnxruntime's own choice.

    onnxruntime sizes its pool from the machine's cores. It does not see a
    container's CPU quota, so under `docker run --cpus 2` on a 64-core host
    it ran 32 threads that the kernel let use two CPUs between them: the
    quota was spent in the first few milliseconds of each period and every
    search then waited, throttled, for the next one. Nor, before Python
    3.13 made it easy to ask, a CPU set narrowed with taskset or cpuset.
    So when either leaves this process at most half of the machine, the
    pool is sized to what it may use. Half, because onnxruntime's own
    choice is the PHYSICAL cores, half the logical ones where each core
    runs two threads: above half, its default already fits on such a
    machine, and setting more threads than that would only add contention.
    """
    total = os.cpu_count() or 1
    usable = total
    count = getattr(os, "process_cpu_count", None)
    try:
        if count is not None:
            usable = count() or total
        elif hasattr(os, "sched_getaffinity"):
            usable = len(os.sched_getaffinity(0)) or total
    except OSError:
        pass
    if sys.platform.startswith("linux"):
        limit = _cgroup_cpu_limit()
        if limit is not None:
            usable = min(usable, max(1, int(limit)))
    return usable if usable <= total // 2 else 0


class Embedder:
    """CLS-pooled, L2-normalized sentence embeddings from a local ONNX model."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        # Before the library loads, or it keeps telemetry: onnxruntime 1.31
        # writes a device id, and for every process that loads a model an
        # event naming the device model, CPU, OS and library versions, to a
        # store for Microsoft's event SDK, whose HTTP uploader is in the
        # same library; on macOS under ~/Library/Application Support/
        # Microsoft/DeveloperTools. Its disable_telemetry_events() does not
        # stop that; this does. A value the user set is left as it is. The
        # embedding daemon builds its models here too, so it sets this in
        # its own process.
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        try:
            import onnxruntime as ort      # local import: keeps CLI startup fast
            from tokenizers import Tokenizer
        except ModuleNotFoundError as exc:
            better = _runtime_not_installed(exc)
            if better is not None:
                raise better from exc
            raise
        except ImportError as exc:
            better = _missing_runtime_error(exc)
            if better is not None:
                raise better from exc
            raise

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
        threads = intra_op_threads()
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(info.onnx), so,
                                         providers=["CPUExecutionProvider"])
        self._needs_token_type = any(
            i.name == "token_type_ids" for i in self.sess.get_inputs())

    def _encode(self, texts: list[str]) -> list[list[int]]:
        """Token ids per text, truncated to the model's 512, never padded."""
        texts = [well_formed(t) for t in texts]
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
        # The windows are cut from the text the tokenizer saw: replacing a
        # surrogate is one code point for one, so its offsets index this.
        text = well_formed(text)
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
