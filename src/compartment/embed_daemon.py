"""One embedding model in RAM for every agent on the machine.

Every MCP client starts its own `compartment serve`, and every one of those
used to load its own copy of the encoder: the onnxruntime, the tokenizer, the
model, and whatever the inference arena grew to. On a laptop running Claude
Desktop, four Claude Code sessions and the menu bar app that was six copies of
the same 50 MB runtime, six arenas, and a search that had to wait for its own
model to load in a process that had been idle for an hour.

This module puts the model in one process. A `compartment serve`, a CLI
command or the app asks `RemoteEmbedder.connect()` for it; the first caller
that finds nobody listening starts the daemon and the rest connect to it. The
daemon loads a model on the first request for it, keeps it while anyone is
connected, and exits a few minutes after the last client hangs up, so a
machine with no agent running pays nothing. The MCP servers keep their vaults,
their keys and their locks exactly as before: the only thing that leaves a
process is text going in and unit vectors coming out.

The wire is a Unix domain socket in the session directory, which is the
user's own 0700 directory that already holds the unlock credential - the
same trust boundary. Newline-delimited JSON, one request per line, vectors
as base64 float32. The daemon accepts a connection only from a process with
its own uid (SO_PEERCRED / LOCAL_PEERCRED), the client connects only to a
socket owned by its own uid, and the two agree on the protocol version, the
package version and the SHA-256 of the model before a single vector crosses.
A daemon left behind by an older install is told to exit by the first newer
client; an older client meeting a newer daemon keeps its own model instead.

Nothing here is a listener in the network sense. `--assert-offline` allows
AF_UNIX, so the offline guard is exactly as strict as it was.

Windows has no AF_UNIX in Python, so there every process keeps its own model
as it always did; `enabled()` says no and `get_embedder` never comes here.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from . import __version__
from .embed import (CHUNK_STRIDE, CHUNK_WINDOW, DEFAULT_MODEL, MAX_CHUNKS,
                    Embedder, ModelError, model_info)
from .home import env

try:
    import fcntl
except ImportError:                                  # Windows
    fcntl = None                                     # type: ignore[assignment]

try:
    import resource
except ImportError:                                  # Windows
    resource = None                                  # type: ignore[assignment]

#: Bumped whenever a request or reply changes shape. Client and daemon must
#: match exactly; there is no negotiation, because both halves ship in the
#: same wheel and a mismatch means two installs are talking.
PROTOCOL = 1

#: Seconds with no client connected before the daemon exits. Long enough
#: that an agent restarting between two tasks finds the model still warm,
#: short enough that a machine nobody is using gets its RAM back.
DEFAULT_IDLE = 300

SOCKET_NAME = "embed.sock"

#: One request line. A `store_many` of a few hundred long records is well
#: under a megabyte; this is a sanity bound against garbage on the socket,
#: not a limit anyone should meet.
_MAX_LINE = 64 * 1024 * 1024

#: AF_UNIX paths are short: 104 bytes on macOS and the BSDs, 108 on Linux,
#: including the terminating NUL. A path over this goes to the temp
#: directory instead.
_MAX_SOCKET_PATH = 100


class Unavailable(RuntimeError):
    """The shared daemon cannot be used; the caller embeds locally."""


# --------------------------------------------------------------- where and if

def supported() -> bool:
    """Whether this platform can run the daemon at all."""
    return (os.name == "posix" and hasattr(socket, "AF_UNIX")
            and fcntl is not None)


def enabled(setting: bool | None = None) -> bool:
    """Whether a vault in this process should use the shared daemon.

    `COMPARTMENT_EMBED_DAEMON=0` (or `off`, `false`, `no`) turns it off for a
    process, which is what tests, containers and anyone debugging want; any
    other value turns it on even where a vault's settings say otherwise. With
    nothing exported, the vault's `embed_daemon` setting decides, and its
    default is on.
    """
    if not supported():
        return False
    flag = env("EMBED_DAEMON")
    if flag is not None and flag.strip() != "":
        return flag.strip().lower() not in ("0", "off", "false", "no")
    return setting is not False


def idle_seconds() -> int:
    try:
        return max(1, int(env("EMBED_IDLE", str(DEFAULT_IDLE))))
    except ValueError:
        return DEFAULT_IDLE


def socket_path() -> Path:
    """Where the daemon listens.

    `COMPARTMENT_EMBED_SOCKET` names it outright. Otherwise it sits in the
    session directory beside the unlock credential, the one private
    directory Compartment already keeps. A home directory deep enough to push
    that past the AF_UNIX limit gets a per-user file in the temp directory,
    named after the home so two homes never share one.
    """
    explicit = env("EMBED_SOCKET")
    if explicit:
        return Path(explicit)
    from . import session
    p = session._session_dir() / SOCKET_NAME
    if _fits(p):
        return p
    tag = base64.urlsafe_b64encode(
        str(Path.home()).encode("utf-8")).decode("ascii").rstrip("=")[-12:]
    return Path(tempfile.gettempdir()) / f"compartment-{os.getuid()}-{tag}.sock"


def _fits(p: Path) -> bool:
    return len(os.fsencode(str(p))) <= _MAX_SOCKET_PATH


# ------------------------------------------------------------------- the wire

def _pack(a: np.ndarray) -> dict:
    a = np.ascontiguousarray(a, dtype=np.float32)
    return {"shape": list(a.shape),
            "b64": base64.b64encode(a.tobytes()).decode("ascii")}


def _unpack(d: dict) -> np.ndarray:
    raw = base64.b64decode(d["b64"])
    return np.frombuffer(raw, dtype=np.float32).reshape(d["shape"]).copy()


def _write(wfile, obj: dict) -> None:
    wfile.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
    wfile.flush()


def _read(rfile) -> dict | None:
    line = rfile.readline(_MAX_LINE + 1)
    if not line:
        return None
    if len(line) > _MAX_LINE:
        raise ValueError("request line too long")
    return json.loads(line.decode("utf-8"))


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


# ------------------------------------------------------------- who is calling

def _peer_uid(conn: socket.socket) -> int | None:
    """The uid of the process on the other end, or None where the platform
    cannot say. Linux answers through SO_PEERCRED, the BSDs and macOS through
    LOCAL_PEERCRED; anything else falls back to the directory permissions."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                  struct.calcsize("iII"))
            _pid, uid, _gid = struct.unpack("iII", raw)
            return uid
        if sys.platform == "darwin" or "bsd" in sys.platform:
            # struct xucred { u_int cr_version; uid_t cr_uid; short
            # cr_ngroups; gid_t cr_groups[16]; } on SOL_LOCAL (0),
            # LOCAL_PEERCRED (1). Version 0 is the only one defined.
            raw = conn.getsockopt(0, 1, 76)
            version, uid = struct.unpack("II", raw[:8])
            if version != 0:
                return None
            return uid
    except (OSError, struct.error):
        return None
    return None


def _owned_socket(p: Path) -> bool:
    """True if `p` is a socket file that belongs to this user."""
    try:
        st = os.stat(p)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


# ------------------------------------------------------------------ the daemon

class _Daemon:
    def __init__(self, path: Path, idle: int):
        self.path = path
        self.idle = idle
        self._models: dict[str, Embedder] = {}
        self._models_lock = threading.Lock()
        self._state = threading.Lock()
        self._clients = 0
        self._last_active = time.monotonic()
        self._started = time.time()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._lock_fd: int | None = None
        self._inode: int | None = None

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._take_lock():
            return 0                    # another daemon owns this socket
        try:
            self.path.unlink()          # a socket file a dead daemon left
        except FileNotFoundError:
            pass
        except OSError:
            return 1
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(self.path))
        except OSError:
            return 1
        os.chmod(self.path, 0o600)
        self._inode = os.stat(self.path).st_ino
        srv.listen(32)
        srv.settimeout(1.0)
        self._listener = srv
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except (ValueError, OSError):
                pass                    # not the main thread
        threading.Thread(target=self._watch, daemon=True).start()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._serve, args=(conn,),
                                 daemon=True).start()
        finally:
            self._cleanup()
        return 0

    def _take_lock(self) -> bool:
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._lock_fd = fd
        return True

    def _cleanup(self) -> None:
        try:
            if self._listener is not None:
                self._listener.close()
        except OSError:
            pass
        # Only remove the socket if it is still ours: a replacement daemon
        # may already be listening on a new file at the same path.
        try:
            if self._inode is not None and os.stat(self.path).st_ino == self._inode:
                self.path.unlink()
        except OSError:
            pass
        if self._lock_fd is not None:
            try:
                os.ftruncate(self._lock_fd, 0)
                os.close(self._lock_fd)         # releases the flock
            except OSError:
                pass

    def _shutdown(self) -> None:
        """Stop accepting: the socket file goes first, so a client that
        arrives during the last milliseconds finds nothing and starts a
        fresh daemon rather than connecting to one that is leaving."""
        self._stop.set()
        try:
            if self._inode is not None and os.stat(self.path).st_ino == self._inode:
                self.path.unlink()
        except OSError:
            pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    def _watch(self) -> None:
        """Exit when idle, and when the socket file is no longer ours: a
        `stop` that swept the files, or a competing daemon that won the
        path, must not leave a process nobody can reach holding a model."""
        while not self._stop.is_set():
            time.sleep(1.0)
            with self._state:
                idle = self._clients == 0 and \
                    time.monotonic() - self._last_active > self.idle
            gone = False
            try:
                gone = os.stat(self.path).st_ino != self._inode
            except OSError:
                gone = True
            if idle or gone:
                self._shutdown()

    # -- connections -------------------------------------------------------
    #: The requests that make a connection a client. A `status` or `stop`
    #: probe is neither counted nor treated as activity, so polling the
    #: daemon does not keep it alive and does not inflate its client count.
    _CLIENT_OPS = frozenset(
        {"hello", "embed_query", "embed_passages", "embed_record", "chunk"})

    def _serve(self, conn: socket.socket) -> None:
        uid = _peer_uid(conn)
        if uid is not None and uid != os.getuid():
            conn.close()
            return
        counted = False
        rfile = conn.makefile("rb")
        wfile = conn.makefile("wb")
        try:
            while not self._stop.is_set():
                try:
                    req = _read(rfile)
                except (ValueError, OSError):
                    break
                if req is None:
                    break
                if req.get("op") in self._CLIENT_OPS:
                    with self._state:
                        if not counted:
                            counted = True
                            self._clients += 1
                        self._last_active = time.monotonic()
                try:
                    _write(wfile, self._handle(req))
                except OSError:
                    break
                if req.get("op") == "shutdown":
                    self._shutdown()
                    break
        finally:
            if counted:
                with self._state:
                    self._clients -= 1
                    self._last_active = time.monotonic()
            for f in (rfile, wfile, conn):
                try:
                    f.close()
                except OSError:
                    pass

    def _embedder(self, name: str) -> Embedder:
        with self._models_lock:
            emb = self._models.get(name)
            if emb is None:
                emb = self._models[name] = Embedder(name)
            return emb

    def _handle(self, req: dict) -> dict:
        op = req.get("op")
        model = req.get("model") or DEFAULT_MODEL
        try:
            if op == "hello":
                info = model_info(model)
                return {"ok": True, "protocol": PROTOCOL, "version": __version__,
                        "pid": os.getpid(), "model": model,
                        "model_sha256": info.sha256, "dim": info.dim}
            if op == "embed_query":
                return {"ok": True,
                        "vec": _pack(self._embedder(model).embed_query(req["text"]))}
            if op == "embed_passages":
                emb = self._embedder(model)
                return {"ok": True, "vecs": _pack(emb.embed_passages(
                    list(req["texts"]), batch=int(req.get("batch", 64))))}
            if op == "embed_record":
                return {"ok": True,
                        "vecs": _pack(self._embedder(model).embed_record(req["text"]))}
            if op == "chunk":
                emb = self._embedder(model)
                return {"ok": True, "chunks": emb.chunk(
                    req["text"], window=int(req.get("window", CHUNK_WINDOW)),
                    stride=int(req.get("stride", CHUNK_STRIDE)),
                    max_chunks=int(req.get("max_chunks", MAX_CHUNKS)))}
            if op == "status":
                return {"ok": True, **self._status()}
            if op == "shutdown":
                return {"ok": True, "pid": os.getpid()}
            return {"ok": False, "kind": "ValueError", "error": f"unknown op {op!r}"}
        except ModelError as exc:
            return {"ok": False, "kind": "ModelError", "error": str(exc)}
        except Exception as exc:                            # noqa: BLE001
            return {"ok": False, "kind": type(exc).__name__, "error": str(exc)}

    def _status(self) -> dict:
        with self._state:
            clients = self._clients
        peak = None
        if resource is not None:
            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak = round(r / (1024 * 1024) if sys.platform == "darwin"
                         else r / 1024)
        return {"running": True, "pid": os.getpid(), "version": __version__,
                "protocol": PROTOCOL, "socket": str(self.path),
                "clients": clients, "models": sorted(self._models),
                "idle_seconds": self.idle,
                "uptime_seconds": round(time.time() - self._started),
                "rss_mb": _current_rss_mb(),
                "footprint_mb": _footprint_mb(), "peak_rss_mb": peak}


def _current_rss_mb() -> int | None:
    """Resident size right now, as distinct from the peak getrusage keeps.

    One long import can push the peak to a few hundred megabytes for the
    seconds a batch is in flight; without an arena that memory goes back
    when the batch ends, and this is the number that shows it did."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", encoding="ascii") as f:
                pages = int(f.read().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5)
        return round(int(out.stdout.strip()) / 1024)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return None


def _footprint_mb() -> int | None:
    """macOS only: the physical footprint, which is what Activity Monitor
    calls Memory. RSS there also counts pages malloc has already handed
    back as reusable, so after a large batch it reads hundreds of megabytes
    high while the footprint has already dropped."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["footprint", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            if "phys_footprint:" in line:
                return round(float(line.split(":")[1].split()[0]))
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        pass
    return None


def run(path: Path | None = None, idle: int | None = None) -> int:
    """Run the daemon in this process until it is idle or told to stop."""
    if not supported():
        print("compartment: the shared embedding daemon needs a POSIX system "
              "with Unix domain sockets", file=sys.stderr)
        return 2
    return _Daemon(path or socket_path(), idle or idle_seconds()).run()


# ------------------------------------------------------------ starting it up

def _connect(path: Path, timeout: float = 5.0) -> socket.socket | None:
    if not _owned_socket(path):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except OSError:
        s.close()
        return None
    # Requests block for as long as a batch takes to embed; a dead daemon
    # closes the connection and readline returns EOF, which is the signal
    # the client acts on. No timeout here.
    s.settimeout(None)
    return s


def _spawn(path: Path, idle: int) -> None:
    """Start the daemon detached: its own session, so neither the MCP
    client's process-group cleanup nor a closing terminal takes it down,
    and no inherited stdio, because stdout of an MCP server IS the
    transport."""
    cmd = [sys.executable, "-m", "compartment.embed_daemon",
           "--socket", str(path), "--idle", str(idle)]
    # The daemon must run the same code as the client that starts it, so it
    # imports the package from wherever this module was imported from - a
    # source checkout under test, an editable install, a bundle - rather
    # than whatever the interpreter would find on its own.
    here = str(Path(__file__).resolve().parents[1])
    env_ = dict(os.environ)
    env_["PYTHONPATH"] = here + (os.pathsep + env_["PYTHONPATH"]
                                 if env_.get("PYTHONPATH") else "")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     close_fds=True, cwd=str(Path.home()), env=env_)


def connect_or_spawn(path: Path, idle: int, *, spawn: bool = True,
                     wait: float = 15.0) -> socket.socket | None:
    """A connected socket to a daemon at `path`, starting one if needed.

    Several MCP servers start within the same second when a client comes
    up, so the spawn is serialised on a lock file: one of them starts the
    daemon, the others wait on the lock and connect to it. Whoever spawned
    it polls until the socket accepts, which is a few hundred milliseconds
    - the daemon binds first and loads the model on the first request.
    """
    s = _connect(path)
    if s is not None or not spawn:
        return s
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".spawn", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    return None
                time.sleep(0.05)
        s = _connect(path)
        if s is not None:
            return s
        try:
            _spawn(path, idle)
        except OSError:
            return None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            s = _connect(path)
            if s is not None:
                return s
        return None
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ------------------------------------------------------------------ the client

class RemoteEmbedder:
    """An Embedder whose model lives in the shared daemon.

    Same attributes, same methods, same vectors. If the daemon disappears
    and cannot be brought back, the instance loads a local model and carries
    on; `shared` then reads False and the vault never noticed.
    """

    def __init__(self, model_name: str, path: Path, *, spawn: bool = True,
                 wait: float = 15.0, idle: int | None = None):
        self.info = model_info(model_name)
        self.model_name = model_name
        self.dim = self.info.dim
        self.prefix_query = self.info.prefix_query
        self.prefix_passage = self.info.prefix_passage
        self.model_sha256 = self.info.sha256
        self._path = path
        self._spawn = spawn
        self._wait = wait
        self._idle = idle or idle_seconds()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        self._pid: int | None = None
        self._local: Embedder | None = None
        self._open()

    @classmethod
    def connect(cls, model_name: str = DEFAULT_MODEL, *, path: Path | None = None,
                spawn: bool = True, wait: float = 15.0) -> "RemoteEmbedder | None":
        """A connected instance, or None when the daemon cannot be used and
        the caller should build a local Embedder instead. A model that is
        not installed is not a daemon problem: None here, and the local
        constructor raises the ModelError that names the remedy."""
        try:
            model_info(model_name)
        except ModelError:
            return None
        try:
            return cls(model_name, path or socket_path(), spawn=spawn, wait=wait)
        except Unavailable:
            return None

    # -- properties the vault reports --------------------------------------
    @property
    def shared(self) -> bool:
        return self._local is None

    @property
    def daemon_pid(self) -> int | None:
        return self._pid if self._local is None else None

    # -- connection management ---------------------------------------------
    def _open(self) -> None:
        for attempt in (1, 2):
            s = connect_or_spawn(self._path, self._idle, spawn=self._spawn,
                                 wait=self._wait)
            if s is None:
                raise Unavailable(f"no embedding daemon at {self._path}")
            self._sock, self._rfile, self._wfile = s, s.makefile("rb"), s.makefile("wb")
            try:
                _write(self._wfile, {"op": "hello", "model": self.model_name})
                hello = _read(self._rfile)
            except (OSError, ValueError) as exc:
                self._drop()
                raise Unavailable(str(exc)) from exc
            if not hello or not hello.get("ok"):
                self._drop()
                raise Unavailable((hello or {}).get("error", "no hello"))
            mine, theirs = _vtuple(__version__), _vtuple(hello.get("version", ""))
            if hello.get("protocol") != PROTOCOL or theirs != mine:
                if theirs < mine and attempt == 1:
                    # An older install left its daemon behind. Ask it to go,
                    # then start ours. The other direction never evicts: a
                    # newer daemon belongs to a newer install, and this older
                    # client simply keeps its own model.
                    self._evict()
                    continue
                self._drop()
                raise Unavailable("daemon is a different version")
            if hello.get("model_sha256") != self.model_sha256:
                self._drop()
                raise Unavailable("daemon serves a different model file")
            self._pid = int(hello.get("pid") or 0) or None
            return
        raise Unavailable("could not replace the older daemon")

    def _evict(self) -> None:
        try:
            _write(self._wfile, {"op": "shutdown"})
            _read(self._rfile)
        except (OSError, ValueError):
            pass
        self._drop()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            probe = _connect(self._path, timeout=0.5)
            if probe is None:
                return
            probe.close()
            time.sleep(0.05)

    def _drop(self) -> None:
        for f in (self._rfile, self._wfile, self._sock):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        self._sock = self._rfile = self._wfile = None
        self._pid = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _call(self, op: str, **fields) -> dict:
        """One round trip, reconnecting once if the daemon went away."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._open()
                    _write(self._wfile, {"op": op, "model": self.model_name, **fields})
                    resp = _read(self._rfile)
                    if resp is None:
                        raise OSError("daemon closed the connection")
                except (OSError, ValueError, Unavailable) as exc:
                    self._drop()
                    if attempt == 2:
                        raise Unavailable(str(exc)) from exc
                    continue
                if resp.get("ok"):
                    return resp
                if resp.get("kind") == "ModelError":
                    raise ModelError(resp.get("error", "model error"))
                raise Unavailable(resp.get("error", "daemon error"))
        raise Unavailable("unreachable")

    def _fallback(self) -> Embedder:
        if self._local is None:
            self._drop()
            self._local = Embedder(self.model_name)
        return self._local

    # -- the Embedder interface --------------------------------------------
    def embed_query(self, text: str) -> np.ndarray:
        if self._local is None:
            try:
                return _unpack(self._call("embed_query", text=text)["vec"])
            except Unavailable:
                pass
        return self._fallback().embed_query(text)

    def embed_passages(self, texts: list[str], batch: int = 64) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        if self._local is None:
            try:
                return _unpack(self._call("embed_passages", texts=texts,
                                          batch=int(batch))["vecs"])
            except Unavailable:
                pass
        return self._fallback().embed_passages(texts, batch=batch)

    def chunk(self, text: str, window: int = CHUNK_WINDOW,
              stride: int = CHUNK_STRIDE, max_chunks: int = MAX_CHUNKS) -> list[str]:
        if self._local is None:
            try:
                return list(self._call("chunk", text=text, window=window,
                                       stride=stride, max_chunks=max_chunks)["chunks"])
            except Unavailable:
                pass
        return self._fallback().chunk(text, window=window, stride=stride,
                                      max_chunks=max_chunks)

    def embed_record(self, text: str) -> np.ndarray:
        if self._local is None:
            try:
                return _unpack(self._call("embed_record", text=text)["vecs"])
            except Unavailable:
                pass
        return self._fallback().embed_record(text)


# ------------------------------------------------------- status, stop, main

def status(path: Path | None = None) -> dict:
    """What the daemon at `path` reports, or that nothing is running."""
    path = path or socket_path()
    if not supported():
        return {"running": False, "socket": str(path),
                "reason": "not supported on this platform"}
    s = _connect(path, timeout=2.0)
    if s is None:
        return {"running": False, "socket": str(path)}
    try:
        w, r = s.makefile("wb"), s.makefile("rb")
        _write(w, {"op": "status"})
        resp = _read(r) or {}
    except (OSError, ValueError):
        return {"running": False, "socket": str(path)}
    finally:
        s.close()
    resp.pop("ok", None)
    return resp


def stop(path: Path | None = None, wait: float = 5.0) -> dict:
    """Ask the daemon to exit and wait until its socket stops answering.

    A daemon that does not answer but whose pid still holds the lock file
    gets SIGTERM. Stale files a crashed daemon left are removed so the next
    client starts cleanly.
    """
    path = path or socket_path()
    if not supported():
        return {"stopped": False, "socket": str(path)}
    pid = None
    s = _connect(path, timeout=2.0)
    if s is not None:
        try:
            w, r = s.makefile("wb"), s.makefile("rb")
            _write(w, {"op": "shutdown"})
            resp = _read(r) or {}
            pid = resp.get("pid")
        except (OSError, ValueError):
            pass
        finally:
            s.close()
    else:
        pid = _locked_pid(path)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pid = None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not _connect_probe(path) and _locked_pid(path) is None:
            break
        time.sleep(0.05)
    if not _connect_probe(path):
        for suffix in ("", ".lock", ".spawn"):
            try:
                Path(str(path) + suffix).unlink()
            except OSError:
                pass
    return {"stopped": pid is not None, "pid": pid, "socket": str(path)}


def _connect_probe(path: Path) -> bool:
    s = _connect(path, timeout=0.5)
    if s is None:
        return False
    s.close()
    return True


def _locked_pid(path: Path) -> int | None:
    """The pid written into the lock file, if a live daemon holds the lock."""
    try:
        fd = os.open(str(path) + ".lock", os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raw = os.read(fd, 32).strip()
            return int(raw) if raw.isdigit() else None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None                     # nobody holds it: no daemon
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="compartment embed-daemon run")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--idle", type=int, default=None)
    args = ap.parse_args(argv)
    return run(Path(args.socket) if args.socket else None, args.idle)


if __name__ == "__main__":
    sys.exit(main())
