"""The doors around the socket constructor.

A guard that only patches `socket.socket.__new__` leaves two ways out that
need no Python socket object at all: the C resolver entry points, which put
caller-chosen bytes on the wire as DNS query labels, and the C type
`_socket.socket` underneath the Python subclass. These tests hold both shut,
and hold the local paths open.

Nothing here connects to anything. Every network assertion is that the call
RAISED, which happens before any packet could be sent.
"""
import inspect
import json
import os
import socket
import subprocess
import sys

import pytest

import _socket

from compartment import offline_guard


@pytest.fixture()
def guarded():
    """Guard on for the test, off again afterwards no matter what happens.

    When the whole suite runs under COMPARTMENT_ASSERT_OFFLINE=1 the guard is
    sealed and stays on, which is what that environment asked for; every test
    in this file is written to pass either way.
    """
    offline_guard.activate()
    try:
        yield
    finally:
        offline_guard.deactivate()


def test_dns_resolution_is_blocked(guarded):
    """The important one: a name lookup exfiltrates in the query labels."""
    with pytest.raises(offline_guard.OfflineViolation):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(offline_guard.OfflineViolation):
        socket.gethostbyname("example.com")
    with pytest.raises(offline_guard.OfflineViolation):
        socket.gethostbyname_ex("example.com")
    with pytest.raises(offline_guard.OfflineViolation):
        socket.gethostbyaddr("8.8.8.8")
    with pytest.raises(offline_guard.OfflineViolation):
        socket.getnameinfo(("8.8.8.8", 80), 0)
    with pytest.raises(offline_guard.OfflineViolation):
        socket.getfqdn()
    with pytest.raises(offline_guard.OfflineViolation):
        socket.create_connection(("example.com", 80), timeout=1)


def test_dns_blocked_through_a_captured_reference(guarded):
    """A library that grabbed `socket.getaddrinfo` before we activated, or
    that calls the C module directly, is on the same leash."""
    with pytest.raises(offline_guard.OfflineViolation):
        _socket.getaddrinfo("example.com", 80)


def test_raw_c_socket_type_is_blocked(guarded):
    """`socket.socket` is a Python subclass; the base class is the other door."""
    with pytest.raises(offline_guard.OfflineViolation):
        _socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(offline_guard.OfflineViolation):
        socket.socket.__bases__[0](socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(offline_guard.OfflineViolation):
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX here")
def test_local_ipc_still_works(guarded, tmp_path, monkeypatch):
    """Local IPC is not networking. A blanket block would break it."""
    # Bound from inside the directory: an AF_UNIX path has ~100 bytes to
    # spend, and a temp directory can eat all of them.
    monkeypatch.chdir(tmp_path)
    path = "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw = _socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        server.listen(1)
        client.connect(path)
        client.sendall(b"local")
        conn, _ = server.accept()
        with conn:
            assert conn.recv(5) == b"local"
    finally:
        raw.close()
        client.close()
        server.close()


def test_literal_and_localhost_lookups_still_resolve(guarded):
    """Resolving something already local asks no one anything, so it stays
    allowed: blocking it would break loopback code paths for no gain."""
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getnameinfo(("127.0.0.1", 80),
                              socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)
    assert socket.gethostname()  # no lookup, just our own name


def test_activate_is_idempotent(guarded):
    before = socket.socket.__new__
    offline_guard.activate()
    offline_guard.activate()
    assert offline_guard.is_active()
    assert socket.socket.__new__ is before
    with pytest.raises(offline_guard.OfflineViolation):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_deactivate_leaves_nothing_behind():
    """The guard must not leak into the tests that run after this file."""
    offline_guard.activate()
    try:
        with pytest.raises(offline_guard.OfflineViolation):
            socket.getaddrinfo("example.com", 80)
    finally:
        turned_off = offline_guard.deactivate()

    if offline_guard.is_sealed():
        # Activated from the environment: refusing is the point, see below.
        assert turned_off is False
        assert offline_guard.is_active()
        return

    assert turned_off is True
    assert not offline_guard.is_active()
    assert socket.socket.__new__ is offline_guard._original_socket_new
    assert _socket.socket is offline_guard._real_raw_socket
    assert socket.socketpair is offline_guard._original_socketpair
    assert socket.getaddrinfo is offline_guard._original_resolvers["getaddrinfo"]
    # AF_INET is buildable again, without connecting it to anything.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.close()


def test_env_activated_guard_cannot_be_switched_off_in_process():
    """`deactivate()` is public and unauthenticated, so a guard the machine
    asked for is sealed: in-process code cannot drop the invariant."""
    sealed_env = os.environ.get("COMPARTMENT_ASSERT_OFFLINE") == "1"
    if not sealed_env:
        assert not offline_guard.is_sealed()
        pytest.skip("only meaningful under COMPARTMENT_ASSERT_OFFLINE=1")
    assert offline_guard.is_sealed()
    assert offline_guard.is_active()
    with pytest.warns(RuntimeWarning):
        assert offline_guard.deactivate() is False
    assert offline_guard.is_active()
    with pytest.raises(offline_guard.OfflineViolation):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def _windows_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM,
                        proto=0):
    """socket.socketpair where _socket has none, which is Windows: CPython
    3.11's own fallback from Lib/socket.py, two AF_INET sockets joined
    through a listener on loopback. Only ever run in a child, installed in
    place of socket.socketpair before compartment is imported, as it is
    there."""
    if family == socket.AF_INET:
        host = "127.0.0.1"
    elif family == socket.AF_INET6:
        host = "::1"
    else:
        raise ValueError("Only AF_INET and AF_INET6 socket address families "
                         "are supported")
    if type != socket.SOCK_STREAM:
        raise ValueError("Only SOCK_STREAM socket type is supported")
    if proto != 0:
        raise ValueError("Only protocol zero is supported")
    lsock = socket.socket(family, type, proto)
    try:
        lsock.bind((host, 0))
        lsock.listen()
        addr, port = lsock.getsockname()[:2]
        csock = socket.socket(family, type, proto)
        try:
            csock.setblocking(False)
            try:
                csock.connect((addr, port))
            except (BlockingIOError, InterruptedError):
                pass
            csock.setblocking(True)
            ssock, _ = lsock.accept()
        except BaseException:
            csock.close()
            raise
    finally:
        lsock.close()
    return (ssock, csock)


#: Under the guard, with the pair built as on Windows: an event loop, an
#: AF_INET socket from another thread while a pair is being made, and one
#: from this thread once it is made.
_EVENT_LOOP = """
import asyncio, json, socket, threading
{fallback}
during = []

def pair_while_another_thread_tries(*args, **kwargs):
    t = threading.Thread(target=lambda: during.append(inet()))
    t.start()
    t.join()
    return _windows_socketpair(*args, **kwargs)
socket.socketpair = pair_while_another_thread_tries
from compartment import offline_guard
offline_guard.activate()

def inet():
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
    except offline_guard.OfflineViolation:
        return "refused"
    return "created"
asyncio.new_event_loop().close()
print(json.dumps({{"during": during, "after": inet()}}))
"""


def _child(code, env):
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       stdin=subprocess.DEVNULL, env=env, timeout=120)
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def test_the_pair_an_event_loop_is_built_from_is_let_through():
    """On Windows asyncio builds every event loop's self-pipe with
    socket.socketpair, which there is two AF_INET sockets on loopback, and
    the guard refused them. Nothing else gets one: not another thread while
    the pair is being made, and not this one once it is made."""
    code = _EVENT_LOOP.format(fallback=inspect.getsource(_windows_socketpair))
    rc, out, err = _child(code, dict(os.environ))
    assert rc == 0, err
    assert json.loads(out) == {"during": ["refused"], "after": "refused"}


#: `compartment serve` with the pair built as on Windows, then whether an
#: AF_INET socket is still refused once it has stopped. The keychain is
#: never asked: it is not this test's to touch.
_SERVE = """
import json, socket, sys
{fallback}
socket.socketpair = _windows_socketpair
from compartment import cli, offline_guard, vault
vault.keychain_get = lambda path: None
try:
    cli.main([*{flag!r}, "--vault", {vault!r}, "serve"])
finally:
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
        after = "created"
    except offline_guard.OfflineViolation:
        after = "refused"
    print(json.dumps({{"after": after}}), file=sys.stderr)
"""


@pytest.mark.parametrize("how", ["--assert-offline",
                                 "COMPARTMENT_ASSERT_OFFLINE=1"])
def test_serve_starts_under_the_guard_where_socketpair_is_inet(how, tmp_path):
    """mcp's stdio transport runs on an asyncio event loop, so with the
    guard on, by the flag or by the environment, `compartment serve` exited
    1 on Windows before reading a request. This one's stdin is empty, so it
    stops at once."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("COMPARTMENT_ASSERT_OFFLINE", "ENGRAM_ASSERT_OFFLINE",
                        "COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE")}
    flag = ["--assert-offline"] if how.startswith("--") else []
    if not flag:
        env["COMPARTMENT_ASSERT_OFFLINE"] = "1"
    code = _SERVE.format(fallback=inspect.getsource(_windows_socketpair),
                         flag=flag, vault=str(tmp_path / "v.vault"))
    rc, out, err = _child(code, env)
    assert rc == 0 and "OfflineViolation" not in err, err
    assert json.loads(err.strip().splitlines()[-1]) == {"after": "refused"}


def test_guard_is_off_at_module_scope_unless_the_env_asked_for_it():
    """A sanity check on the file itself: if any test above leaked, this fails
    and says so plainly rather than breaking an unrelated test later."""
    if os.environ.get("COMPARTMENT_ASSERT_OFFLINE") == "1":
        assert offline_guard.is_active()
    else:
        assert not offline_guard.is_active(), "a test in this file leaked the guard"
    assert sys.modules["_socket"] is _socket
