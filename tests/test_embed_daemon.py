"""One embedding model for every agent on the machine.

Each MCP client starts its own `compartment serve`; these make sure that all
of them end up asking one daemon for vectors, that the daemon answers with
exactly what an in-process encoder would, that it starts itself and goes
away on its own, and that a vault never notices when it is missing.
"""
import json
import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from compartment import cli, embed_daemon
from compartment.embed import Embedder, get_embedder
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


def test_a_client_survives_the_daemon_being_killed(daemon_env):
    r = RemoteEmbedder.connect()
    first = r.daemon_pid
    os.kill(first, signal.SIGKILL)
    assert _wait_until(lambda: not embed_daemon.status(daemon_env).get("running"), 10)
    vec = r.embed_query("still here")               # reconnects and respawns
    assert vec.shape == (r.dim,)
    assert r.shared and r.daemon_pid and r.daemon_pid != first


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
