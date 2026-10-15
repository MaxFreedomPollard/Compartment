"""The MCP server around the vault: the auto-lock thread and the panic lock.

No event loop is started here. A tool's body is what runs on the server's
worker thread, so a test calls it through `__wrapped__` (functools.wraps
keeps it), and asyncio's self-pipe is a loopback socket on Windows, which
the offline guard the suite runs under refuses."""
import json
import sqlite3
import time
import types

import pytest

from compartment import server
from compartment.vault import Vault


class _Stop(BaseException):
    """Ends a background loop from inside its sleep. Not an Exception: the
    loops catch Exception, so that a failed pass cannot end them."""


def _run_autolock(monkeypatch, passes):
    """server._autolock_loop for `passes` passes, its sleep returning at once,
    then ended at the next sleep."""
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > passes:
            raise _Stop
    monkeypatch.setattr(server, "time",
                        types.SimpleNamespace(sleep=sleep, time=time.time))
    with pytest.raises(_Stop):
        server._autolock_loop()


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
