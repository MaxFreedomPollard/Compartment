"""The MCP server around the vault: the auto-lock thread and the panic lock.

No event loop is started here. A tool's body is what runs on the server's
worker thread, so a test calls it through `__wrapped__` (functools.wraps
keeps it), and asyncio's self-pipe is a loopback socket on Windows, which
the offline guard the suite runs under refuses."""
import json
import sqlite3
import threading
import time
import types

import pytest

from compartment import server
from compartment.vault import Vault


class _Stop(BaseException):
    """Ends a background loop from inside its sleep. Not an Exception: the
    loops catch Exception, so that a failed pass cannot end them."""


def _fake_sleeps(monkeypatch, passes):
    """server._autolock_loop's sleep returns at once `passes` times, then
    ends the loop."""
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > passes:
            raise _Stop
    monkeypatch.setattr(server, "time",
                        types.SimpleNamespace(sleep=sleep, time=time.time))


def _run_autolock(monkeypatch, passes):
    _fake_sleeps(monkeypatch, passes)
    with pytest.raises(_Stop):
        server._autolock_loop()


def _autolock_thread():
    def run():
        try:
            server._autolock_loop()
        except _Stop:
            pass
    # A daemon, so that a failing test can never leave it holding the run open.
    return threading.Thread(target=run, name="autolock", daemon=True)


@pytest.fixture()
def serving(monkeypatch, vault, vault_path):
    """The server's state with this vault open and idle past its auto-lock."""
    for key, value in (("vault", vault), ("path", vault_path),
                       ("caller", "agent"), ("auto_lock_min", 30),
                       ("last_op", time.time() - 31 * 60)):
        monkeypatch.setitem(server._state, key, value)
    return vault


def _written_by_another_process(v, vault_path):
    other = Vault.unlock(vault_path, raw_key=v._master)
    other.store("stored by another process meanwhile", caller="cli")


def _assert_holds_no_plaintext(v):
    with pytest.raises(sqlite3.ProgrammingError):
        v.db.conn.execute("SELECT text FROM fts")
    assert v.index is None and v._id_by_ikey == {}


def test_auto_lock_drops_the_key_after_another_process_wrote(
        serving, vault_path, monkeypatch, capsys):
    """One write by the CLI or another agent made every later save of the
    server's handle fail, and lock() kept the key whenever its save failed:
    auto-lock reported the failure every 30 seconds and the key stayed in
    RAM for as long as the server stayed idle."""
    v = serving
    v.store("stored before the server went idle", caller="agent")
    _written_by_another_process(v, vault_path)
    _run_autolock(monkeypatch, passes=3)
    assert v._locked and v._master is None
    _assert_holds_no_plaintext(v)
    err = capsys.readouterr().err
    assert err.count("auto-lock failed") == 1, err
    assert "VaultStaleError" in err


def test_the_panic_lock_still_reports_a_flush_it_could_not_make(
        serving, vault_path, monkeypatch):
    from compartment import session
    from compartment import vault as vault_module
    monkeypatch.setattr(session, "clear", lambda path: False)
    monkeypatch.setattr(vault_module, "keychain_clear", lambda path: False)
    v = serving
    _written_by_another_process(v, vault_path)
    out = json.loads(server.memory_lock.__wrapped__())
    assert out["locked"] is True
    assert any(p.startswith("flush failed, key dropped anyway: VaultStaleError")
               for p in out["partial_failure"]), out
    assert v._master is None
    _assert_holds_no_plaintext(v)


def test_dropping_the_key_lets_go_of_the_decrypted_memories(vault):
    """The panic path's own drop, for when lock() itself fails."""
    vault.store("The spare key is under the blue flowerpot", caller="test")
    server._drop_key(vault)
    assert vault._locked and vault._master is None
    _assert_holds_no_plaintext(vault)


class _WaitedGate:
    """server._toolgate, saying when a caller had to wait at it."""

    def __init__(self, waited: threading.Event):
        self._lock = threading.Lock()
        self._waited = waited

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._waited.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


def _call(tool, *args, **kwargs):
    """A tool call as the server makes one: its body under the tool gate."""
    with server._toolgate:
        return tool.__wrapped__(*args, **kwargs)


def test_a_tool_call_that_meets_the_auto_lock_reopens_the_vault(
        serving, monkeypatch):
    """The auto-lock thread locked without the tool gate. A call arriving
    while that lock was saving took the still-open handle from _vault(),
    waited for the vault, and found it locked: VaultLockedError, although
    the stored credential would have reopened it."""
    v = serving
    v.store("The staging database runs Postgres 16", caller="agent")
    key = v._master
    monkeypatch.setattr(Vault, "resolve_credential",
                        staticmethod(lambda path, passphrase=None: (None, key)))
    arrived = threading.Event()        # the call holds a handle, or waits
    saving = threading.Event()
    monkeypatch.setattr(server, "_toolgate", _WaitedGate(arrived))
    real_vault, real_save = server._vault, Vault.save

    def handed_out():
        out = real_vault()
        arrived.set()
        return out

    def save(self, *args, **kwargs):
        if threading.current_thread().name == "autolock":
            saving.set()
            arrived.wait(10)           # the lock saves while the call comes in
        return real_save(self, *args, **kwargs)
    monkeypatch.setattr(server, "_vault", handed_out)
    monkeypatch.setattr(Vault, "save", save)
    _fake_sleeps(monkeypatch, passes=1)
    t = _autolock_thread()
    t.start()
    try:
        assert saving.wait(10)
        out = json.loads(_call(server.memory_search,
                               "which Postgres version does staging run"))
    finally:
        t.join(10)
    assert v._locked                   # the auto-lock did lock the idle handle
    assert any("Postgres 16" in r["text"] for r in out["results"]), out


def test_the_auto_lock_waits_for_a_tool_call_in_progress(serving, monkeypatch):
    """An auto-lock waking while a call runs locks only if the vault is still
    idle once that call has finished."""
    v = serving
    waited = threading.Event()
    monkeypatch.setattr(server, "_toolgate", _WaitedGate(waited))
    _fake_sleeps(monkeypatch, passes=1)
    t = _autolock_thread()
    try:
        with server._toolgate:         # a tool call is running
            t.start()
            while t.is_alive() and not waited.is_set():
                t.join(0.01)
            server._state["last_op"] = time.time()     # as its _vault() does
    finally:
        t.join(10)
    assert not v._locked
