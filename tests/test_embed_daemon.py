"""One embedding model for every agent on the machine.

Each MCP client starts its own `compartment serve`; these make sure that all
of them end up asking one daemon for vectors, that the daemon answers with
exactly what an in-process encoder would, that it starts itself and goes
away on its own, and that a vault never notices when it is missing.
"""
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from compartment import cli, embed_daemon, offline_guard
from compartment.embed import (DEFAULT_MODEL, Embedder, ModelError,
                               get_embedder, model_info)
from compartment.embed_daemon import RemoteEmbedder
from compartment.vault import Vault

from conftest import PASS

pytestmark = pytest.mark.skipif(not embed_daemon.supported(),
                                reason="the daemon needs Unix domain sockets")


@pytest.fixture()
def daemon_env(monkeypatch):
    """A daemon of this test's own, on a short socket path, gone afterwards.

    pytest's tmp_path is too deep for an AF_UNIX path on macOS, so the
    socket lives directly under the system temp directory."""
    d = tempfile.mkdtemp(prefix="cmpt-")
    sock = Path(d) / "embed.sock"
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "1")
    monkeypatch.setenv("COMPARTMENT_EMBED_SOCKET", str(sock))
    monkeypatch.setenv("COMPARTMENT_EMBED_IDLE", "2")
    yield sock
    embed_daemon.stop(sock)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def local():
    return Embedder()


def _wait_until(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ------------------------------------------------------------- same vectors --
def test_the_daemon_answers_with_the_in_process_vectors(daemon_env, local):
    r = RemoteEmbedder.connect()
    assert r is not None and r.shared and r.daemon_pid
    assert (r.dim, r.model_sha256, r.prefix_query, r.prefix_passage) == (
        local.dim, local.model_sha256, local.prefix_query, local.prefix_passage)
    q = "where did I put the router passphrase"
    assert np.array_equal(r.embed_query(q), local.embed_query(q))
    texts = ["short", "a much longer text about a vault " * 30, "mid " * 5, ""]
    assert np.array_equal(r.embed_passages(texts), local.embed_passages(texts))
    long = "sentence about vaults and agents. " * 400
    assert r.chunk(long) == local.chunk(long)
    assert len(r.chunk(long)) > 1
    assert np.array_equal(r.embed_record(long), local.embed_record(long))
    assert r.embed_passages([]).shape == (0, r.dim)


# ------------------------------------------------------------ one of them --
def test_several_clients_share_one_daemon(daemon_env):
    clients = [RemoteEmbedder.connect() for _ in range(4)]
    assert all(c is not None for c in clients)
    assert len({c.daemon_pid for c in clients}) == 1
    st = embed_daemon.status(daemon_env)
    assert st["running"] and st["clients"] == 4 and st["pid"] == clients[0].daemon_pid
    for c in clients:
        c.close()
    assert _wait_until(lambda: embed_daemon.status(daemon_env).get("clients") == 0)


def test_clients_arriving_together_start_exactly_one_daemon(daemon_env):
    """Six MCP servers start within the same second when a client comes up;
    the spawn lock makes one of them start the daemon and the rest wait."""
    results = []

    def go():
        results.append(RemoteEmbedder.connect())

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 6 and all(r is not None for r in results)
    assert len({r.daemon_pid for r in results}) == 1
    assert all(r.embed_query("x").shape == (r.dim,) for r in results)


def test_the_daemon_starts_fast_and_loads_the_model_on_demand(daemon_env):
    t0 = time.monotonic()
    r = RemoteEmbedder.connect()
    assert r is not None
    connected_in = time.monotonic() - t0
    st = embed_daemon.status(daemon_env)
    assert st["models"] == []                      # nothing loaded yet
    r.embed_query("now")
    assert embed_daemon.status(daemon_env)["models"] == [r.model_name]
    assert connected_in < 10


# ------------------------------------------------------------- lifecycle --
def test_the_daemon_exits_when_nobody_is_connected(daemon_env):
    r = RemoteEmbedder.connect()
    assert r is not None
    r.close()
    assert _wait_until(lambda: not embed_daemon.status(daemon_env).get("running"), 20)
    assert _wait_until(lambda: not daemon_env.exists(), 5)


def test_a_client_arriving_as_an_idle_daemon_exits_starts_the_next_one(
        daemon_env, monkeypatch):
    """An idle daemon takes its socket file away first, so that a client
    arriving in its last moments starts a fresh one. It kept its lock until
    its accept loop next woke, up to a second later, so the fresh one found
    the lock held and exited, and that client waited out its 15 s and
    loaded a model of its own."""
    monkeypatch.setenv("COMPARTMENT_EMBED_IDLE", "1")
    first = RemoteEmbedder.connect()
    pid = first.daemon_pid
    first.close()
    time.sleep(0.5)
    # Any connection restarts the accept loop's one-second wait, which
    # puts its wakeups out of step with the idle check's.
    embed_daemon.status(daemon_env)
    assert _wait_until(lambda: not daemon_env.exists(), 10)
    late = RemoteEmbedder.connect()
    assert late is not None and late.shared and late.daemon_pid != pid


def test_a_client_survives_the_daemon_being_killed(daemon_env):
    r = RemoteEmbedder.connect()
    first = r.daemon_pid
    os.kill(first, signal.SIGKILL)
    assert _wait_until(lambda: not embed_daemon.status(daemon_env).get("running"), 10)
    vec = r.embed_query("still here")               # reconnects and respawns
    assert vec.shape == (r.dim,)
    assert r.shared and r.daemon_pid and r.daemon_pid != first


def _returns_within(seconds, fn):
    """What fn returns, or a failure if it is still waiting after `seconds`."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail(f"still waiting after {seconds} s")
    return out[0]


def test_a_daemon_that_stops_answering_does_not_hang_its_clients(
        daemon_env, monkeypatch, local):
    """A daemon that is alive but not serving (suspended with Ctrl-Z, stuck
    under a debugger) still completes connect() through the kernel backlog
    and never closes the connection. The hello and every request waited for
    its reply with no limit, and in the server the waiting call holds the
    tool gate, so every memory tool hung behind it."""
    monkeypatch.setattr(embed_daemon, "_HELLO_TIMEOUT", 0.3, raising=False)
    monkeypatch.setattr(embed_daemon, "_REQUEST_TIMEOUT", 0.3, raising=False)
    r = RemoteEmbedder.connect()
    r.embed_query("warm up")
    pid = r.daemon_pid
    q = "where is the router passphrase"
    os.kill(pid, signal.SIGSTOP)
    try:
        vec = _returns_within(30, lambda: r.embed_query(q))
        fresh = _returns_within(30, get_embedder)
    finally:
        os.kill(pid, signal.SIGCONT)
    assert not r.shared                          # it went on with its own model
    assert np.array_equal(vec, local.embed_query(q))
    assert isinstance(fresh, Embedder)


def test_a_dead_socket_file_is_replaced(daemon_env):
    daemon_env.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(daemon_env))
    s.close()                                       # a file nobody listens on
    assert daemon_env.exists()
    r = RemoteEmbedder.connect()
    assert r is not None and r.embed_query("x").shape == (r.dim,)


def test_stop_ends_it_and_status_says_so(daemon_env):
    r = RemoteEmbedder.connect()
    pid = r.daemon_pid
    out = embed_daemon.stop(daemon_env)
    assert out["stopped"] and out["pid"] == pid
    assert not embed_daemon.status(daemon_env).get("running")
    assert not daemon_env.exists()
    assert r.embed_query("after stop").shape == (r.dim,)   # starts a new one


def test_status_and_stop_from_the_command_line(daemon_env, capsys):
    r = RemoteEmbedder.connect()
    assert r is not None
    cli.main(["embed-daemon", "status"])
    st = json.loads(capsys.readouterr().out)
    assert st["running"] and st["pid"] == r.daemon_pid and st["enabled"] is True
    assert st["clients"] == 1 and "rss_mb" in st
    cli.main(["embed-daemon", "stop"])
    assert json.loads(capsys.readouterr().out)["stopped"] is True
    cli.main(["embed-daemon"])                      # bare: status
    assert json.loads(capsys.readouterr().out)["running"] is False


# ------------------------------------------------------------ versions --
def test_an_older_client_leaves_a_newer_daemon_alone(daemon_env, monkeypatch):
    keep = RemoteEmbedder.connect()                 # the "newer" daemon
    pid = keep.daemon_pid
    monkeypatch.setattr(embed_daemon, "__version__", "0.1")
    assert RemoteEmbedder.connect() is None          # keeps its own model
    assert embed_daemon.status(daemon_env)["pid"] == pid


def test_a_newer_client_replaces_an_older_daemon(daemon_env, monkeypatch):
    old = RemoteEmbedder.connect()
    pid = old.daemon_pid
    monkeypatch.setattr(embed_daemon, "__version__", "999.0")
    # The daemon it starts reports the real version, so the newer client
    # ends up without a daemon and gets a local model - but the older
    # daemon has been told to go.
    assert RemoteEmbedder.connect() is None
    assert _wait_until(lambda: embed_daemon.status(daemon_env).get("pid") != pid, 10)


#: The daemon an older install left running: the shipped one, reporting an
#: older version, and going as earlier releases go when told to: its socket
#: file at once, its lock only when the process exits, which was up to a
#: second later. Here it is always a second.
_OLDER_DAEMON = """
import os, sys, time
from compartment import embed_daemon
embed_daemon.__version__ = "4.0.0"

def shutdown(self):
    if not self._stop.is_set():
        self.told = time.monotonic()
        self._stop.set()
    try:
        if os.stat(self.path).st_ino == self._inode:
            os.unlink(self.path)
    except OSError:
        pass
    self._listener.close()

def cleanup(self):
    time.sleep(max(0.0, self.told + 1.0 - time.monotonic()))

embed_daemon._Daemon._shutdown = shutdown
embed_daemon._Daemon._cleanup = cleanup
sys.exit(embed_daemon.main(sys.argv[1:]))
"""


def test_a_newer_client_takes_over_from_an_older_daemon_at_once(daemon_env):
    """Every upgrade meets the daemon the older install left running. The
    newer client told it to go and started its own as soon as its socket
    file went; the old one still held its lock, so the new one exited, and
    the client waited out its 15 s and loaded a model of its own."""
    env = dict(os.environ, PYTHONPATH=str(
        Path(embed_daemon.__file__).resolve().parents[1]))
    old = subprocess.Popen(
        [sys.executable, "-c", _OLDER_DAEMON, "--socket", str(daemon_env),
         "--idle", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    try:
        assert _wait_until(
            lambda: embed_daemon.status(daemon_env).get("version") == "4.0.0")
        t0 = time.monotonic()
        r = RemoteEmbedder.connect()
        assert r is not None and r.shared and r.daemon_pid != old.pid
        assert time.monotonic() - t0 < 10
        assert old.wait(10) == 0
    finally:
        if old.poll() is None:
            old.kill()
            old.wait()


# ------------------------------------------------------- the offline guard --
@pytest.fixture()
def guarded_by_the_flag(monkeypatch):
    """The guard on as `--assert-offline` turns it on: in this process only,
    with nothing in the environment for a child to inherit."""
    for name in ("COMPARTMENT_ASSERT_OFFLINE", "ENGRAM_ASSERT_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    was_on = offline_guard.is_active()
    offline_guard.activate()
    yield
    if not was_on:
        offline_guard.deactivate()


def _hello(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    try:
        embed_daemon._write(s.makefile("wb"), {"op": "hello"})
        return embed_daemon._read(s.makefile("rb"))
    finally:
        s.close()


def test_the_daemon_a_guarded_process_starts_is_guarded(daemon_env,
                                                        guarded_by_the_flag):
    """The flag guarded the server and the CLI, and the daemon they started,
    the process that loads onnxruntime and runs every embedding, could
    create internet sockets. Its hello says whether its own guard is on."""
    r = RemoteEmbedder.connect()
    assert r is not None and r.shared
    assert _hello(daemon_env)["offline_guard"] is True


@contextlib.contextmanager
def _a_daemon_answering(path, hello):
    """Something at the daemon's socket that answers every request with
    `hello`, while the block runs."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    srv.settimeout(0.2)
    done = threading.Event()

    def serve():
        while not done.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                rfile, wfile = conn.makefile("rb"), conn.makefile("wb")
                try:
                    while embed_daemon._read(rfile) is not None:
                        embed_daemon._write(wfile, hello)
                except (OSError, ValueError):
                    pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join(5)
        srv.close()


def test_a_guarded_client_keeps_its_own_model_beside_an_unguarded_daemon(
        daemon_env, guarded_by_the_flag):
    """The daemon is shared, so a guarded server could send its texts to a
    daemon an unguarded process had started. This one passes every other
    check of the handshake."""
    info = model_info(DEFAULT_MODEL)
    unguarded = {"ok": True, "protocol": embed_daemon.PROTOCOL,
                 "version": embed_daemon.__version__, "pid": os.getpid(),
                 "model": DEFAULT_MODEL, "model_sha256": info.sha256,
                 "dim": info.dim, "offline_guard": False}
    with _a_daemon_answering(daemon_env, unguarded):
        assert isinstance(get_embedder(), Embedder)
        assert RemoteEmbedder.connect() is None


# ------------------------------------------------------------ switches --
def test_the_environment_variable_decides(monkeypatch):
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "0")
    assert embed_daemon.enabled() is False
    assert embed_daemon.enabled(True) is False       # env wins over the vault
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "1")
    assert embed_daemon.enabled(False) is True
    monkeypatch.delenv("COMPARTMENT_EMBED_DAEMON")
    assert embed_daemon.enabled() is True
    assert embed_daemon.enabled(False) is False      # the vault's setting


def test_disabled_means_a_local_encoder(monkeypatch):
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "0")
    assert isinstance(get_embedder(), Embedder)


def test_enabled_means_the_shared_one(daemon_env):
    e = get_embedder()
    assert isinstance(e, RemoteEmbedder) and e.shared


def test_a_socket_path_too_deep_for_af_unix_moves_to_the_temp_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPARTMENT_EMBED_SOCKET", raising=False)
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / ("deep" * 40)))
    p = embed_daemon.socket_path()
    assert len(os.fsencode(str(p))) <= embed_daemon._MAX_SOCKET_PATH
    assert p.name.startswith("compartment-") and p.suffix == ".sock"


def test_a_daemon_that_cannot_listen_is_not_waited_for(monkeypatch, tmp_path):
    """With the session directory and the temp directory both too deep for
    an AF_UNIX path, the daemon a client starts cannot bind and exits at
    once, and the client went on polling for its 15 s, at every process
    start and every reopen of a vault, before using a model of its own."""
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "1")
    monkeypatch.setenv("COMPARTMENT_EMBED_IDLE", "1")
    monkeypatch.delenv("COMPARTMENT_EMBED_SOCKET", raising=False)
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / ("deep" * 40)))
    deep_tmp = tmp_path / ("tmp" * 40)
    deep_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(deep_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(deep_tmp))
    t0 = time.monotonic()
    e = get_embedder()
    took = time.monotonic() - t0
    assert isinstance(e, Embedder)
    assert took < 5
    assert list(deep_tmp.iterdir()) == []       # nothing for a socket that cannot be


def test_the_peer_credential_is_this_user():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert embed_daemon._peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------- the vault --
def test_a_vault_searches_identically_through_the_daemon(daemon_env, tmp_path, monkeypatch):
    vp = str(tmp_path / "shared.vault")
    v = Vault.create(vp, PASS, creator="test")
    assert v.embedder.shared
    st = v.status()
    assert st["embedder"]["mode"] == "shared daemon" and st["embedder"]["daemon_pid"]
    assert st["embedder"]["shared_daemon"] is True
    v.store("The router passphrase is taped under the desk", caller="test",
            source="test")
    v.store("Lunch on Fridays is at the noodle place", caller="test", source="test")
    through = v.search("where is the router passphrase", caller="test")["results"]
    v.lock()
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "0")
    v2 = Vault.unlock(vp, passphrase=PASS)
    assert v2.status()["embedder"]["shared_daemon"] is False
    alone = v2.search("where is the router passphrase", caller="test")["results"]
    assert not v2.embedder.__class__ is RemoteEmbedder
    assert [r["id"] for r in through] == [r["id"] for r in alone]
    assert np.allclose([r["score"] for r in through], [r["score"] for r in alone])
    assert "router" in through[0]["text"]


def test_the_vault_setting_can_turn_it_off(daemon_env, tmp_path, monkeypatch):
    monkeypatch.delenv("COMPARTMENT_EMBED_DAEMON")
    vp = str(tmp_path / "local.vault")
    v = Vault.create(vp, PASS, creator="test")
    v.config.settings["embed_daemon"] = False
    v._embedder = None
    assert not getattr(v.embedder, "shared", False)
    assert v.status()["embedder"]["shared_daemon"] is False


@pytest.fixture()
def read_only_home(tmp_path, monkeypatch):
    """HOME a directory nothing can be made in, as for a service account or
    in a hardened container: the session directory cannot exist."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("COMPARTMENT_SESSION_DIR")
    monkeypatch.delenv("COMPARTMENT_EMBED_SOCKET", raising=False)
    os.chmod(home, 0o500)
    yield home
    os.chmod(home, 0o700)


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root ignores a directory's permissions")
def test_a_home_that_cannot_hold_the_socket_embeds_with_its_own_model(
        tmp_path, monkeypatch, read_only_home):
    """The daemon's socket sits in the session directory, and making that
    directory raised PermissionError straight out of get_embedder, so with
    the daemon on, the default, a vault opened with its passphrase failed
    every search, store and create it ran with the daemon off."""
    vp = str(tmp_path / "v.vault")
    v = Vault.create(vp, PASS, creator="test")
    v.store("The router passphrase is taped under the desk", caller="test",
            source="test")
    v.lock()
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "1")
    v = Vault.unlock(vp, passphrase=PASS)
    found = v.search("where is the router passphrase", caller="test")["results"]
    assert "router" in found[0]["text"]
    assert isinstance(v.embedder, Embedder)
    # A model that is not installed is still the error that names the remedy.
    with pytest.raises(ModelError, match="download-model"):
        get_embedder("multilingual-e5-small-int8")
