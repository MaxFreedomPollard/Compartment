"""The MCP server around the vault: the auto-lock thread and the panic lock.

No event loop is started here. A tool's body is what runs on the server's
worker thread, so a test calls it through `__wrapped__` (functools.wraps
keeps it), and asyncio's self-pipe is a loopback socket on Windows, which
the offline guard the suite runs under refuses."""
import json
import os
import signal
import sqlite3
import threading
import time
import types

import pytest

from compartment import retag, server, vaultfile
from compartment.vault import Vault, strip_provenance

from conftest import fill_clustered


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
                        types.SimpleNamespace(sleep=sleep, time=time.time,
                                              monotonic=time.monotonic))


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
    """The server's state with this vault open and idle past its auto-lock,
    its credential as an open records it."""
    from compartment import session
    for key, value in (("vault", vault), ("path", vault_path),
                       ("caller", "agent"), ("auto_lock_min", 30),
                       ("last_op", time.time() - 31 * 60),
                       ("settings_sig", None),
                       ("cred_sig", session.watch_signature(vault_path)),
                       ("cred_checked", time.monotonic())):
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
    assert err.count("auto-locked the vault") == 1, err
    assert "audit rows were not saved" in err and "VaultStaleError" in err


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


def _retag_thread():
    def run():
        try:
            server._retag_loop()
        except _Stop:
            pass
    return threading.Thread(target=run, name="retag", daemon=True)


def test_a_shred_while_the_retag_pass_lists_its_targets_completes(
        serving, monkeypatch):
    """The background retag pass listed its targets outside the tool gate,
    stepping a SELECT on the connection every tool call uses, and SQLite
    refuses a VACUUM while a statement is open: a memory_forget(shred=true)
    landing then failed after deleting the row, with nothing audited or
    journaled, and a retry found no record left to shred."""
    from compartment import vault as vault_module
    v = serving
    ids = [v.store(f"Memory number {i} about the office.", caller="agent")["id"]
           for i in range(3)]
    listing, go = threading.Event(), threading.Event()
    real_is_seeded = vault_module.is_seeded

    def is_seeded(tags):               # retag.targets asks it row by row
        if threading.current_thread().name == "retag" and not listing.is_set():
            listing.set()
            go.wait(10)                # the shred arrives mid-listing
        return real_is_seeded(tags)
    monkeypatch.setattr(vault_module, "is_seeded", is_seeded)
    # A call that has to wait at the gate lets the listing finish.
    monkeypatch.setattr(server, "_toolgate", _WaitedGate(go))
    monkeypatch.setitem(server._state, "last_retag", 0.0)
    _fake_sleeps(monkeypatch, passes=1)
    t = _retag_thread()
    t.start()
    try:
        assert listing.wait(10)
        try:
            with server._toolgate:     # memory_forget, as the server runs it
                v.forget(ids[1], caller="agent", shred=True)
        finally:
            go.set()
    finally:
        t.join(10)
    assert v.db.get_row(ids[1]) is None
    assert ids[1] not in v._id_by_ikey.values()
    assert v.db.conn.execute(
        "SELECT count(*) FROM audit WHERE op = 'forget' AND detail LIKE ?",
        (f"%{ids[1]}%",)).fetchone()[0] == 1


class _CountingGate:
    """server._toolgate, counting how many times it was taken."""

    def __init__(self):
        self._lock = threading.Lock()
        self.entered = 0

    def __enter__(self):
        self._lock.acquire()
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()


def _tags_now(v):
    return {r["id"]: sorted(json.loads(r["tags"]))
            for r in v.db.conn.execute("SELECT id, tags FROM records")}


def _one_retag_pass(monkeypatch, gate=None):
    if gate is not None:
        monkeypatch.setattr(server, "_toolgate", gate)
    monkeypatch.setitem(server._state, "last_retag", 0.0)
    _fake_sleeps(monkeypatch, passes=1)
    with pytest.raises(_Stop):
        server._retag_loop()


def test_a_background_retag_pass_gives_what_one_whole_vault_plan_gives(
        serving, monkeypatch):
    """The pass retags in slices, releasing the tool gate between them, and
    planned each slice after applying the one before: a tag one slice added
    was a vote in the next, so tags spread further than a plan spreads
    them, and which records got them depended on where a record's random id
    put it in the slicing. Slicing must not change what a record gets."""
    v = serving
    fill_clustered(v, 2000)
    expected = {**_tags_now(v),
                **{c.record_id: sorted(c.after) for c in retag.plan(v)}}
    gate = _CountingGate()
    _one_retag_pass(monkeypatch, gate)
    assert _tags_now(v) == expected
    slices = -(-len(retag.targets(v)) // server.RETAG_CHUNK)
    assert slices >= 5 and gate.entered >= slices + 1   # one slice at a time


def test_tags_a_tool_call_writes_between_two_slices_are_kept(serving,
                                                            monkeypatch):
    v = serving
    fill_clustered(v, 200)
    monkeypatch.setattr(server, "RETAG_CHUNK", 50)
    late = retag.targets(v)[-1]                 # planned in the last slice
    real_apply, calls = retag.apply, []

    def apply(vault, changes, caller="retag"):
        n = real_apply(vault, changes, caller=caller)
        if not calls:                           # a tool call after slice one
            vault.db.set_tags(late, ["chosen-by-a-tool-call"])
        calls.append(n)
        return n
    monkeypatch.setattr(retag, "apply", apply)
    _one_retag_pass(monkeypatch)
    assert len(calls) == 4
    assert "chosen-by-a-tool-call" in _tags_now(v)[late]


def _encoders_built(monkeypatch):
    """Every Embedder this process constructs from here on."""
    from compartment import embed
    built, real_init = [], embed.Embedder.__init__

    def init(self, *args, **kwargs):
        built.append(self)
        real_init(self, *args, **kwargs)
    monkeypatch.setattr(embed.Embedder, "__init__", init)
    return built


def _written_with_a_vector(vault_path, key, n):
    """A write by another process that needs no model in this one."""
    import numpy as np
    vec = np.random.default_rng(n).standard_normal(384).astype(np.float32)
    Vault.unlock(vault_path, raw_key=key).store(
        f"Another agent wrote this, number {n}", caller="cli",
        vec=vec / np.linalg.norm(vec))


def test_a_reopen_after_another_process_writes_keeps_the_model(
        serving, vault_path, monkeypatch):
    """Without the shared daemon, which is every Windows machine, each
    reopen after another process wrote the vault built the encoder again,
    an ONNX session and a tokenizer, at the next search: six for five
    foreign writes."""
    v = serving
    v.store("The staging database runs Postgres 16", caller="agent")
    key = v._master
    monkeypatch.setattr(Vault, "resolve_credential",
                        staticmethod(lambda path, passphrase=None: (None, key)))
    built = _encoders_built(monkeypatch)
    handles = []
    for n in range(5):
        _written_with_a_vector(vault_path, key, n)
        out = json.loads(_call(server.memory_search,
                               "which Postgres version does staging run"))
        assert any("Postgres 16" in r["text"] for r in out["results"])
        handles.append(server._state["vault"])
    assert len({id(h) for h in handles + [v]}) == 6     # five reopens
    assert built == []


def test_a_reopen_builds_its_own_model_when_the_pin_moved(
        serving, vault_path, monkeypatch):
    """Another process may have re-embedded the vault with another model,
    and then the encoder in hand is the wrong one."""
    v = serving
    v.store("The staging database runs Postgres 16", caller="agent")
    key = v._master
    monkeypatch.setattr(Vault, "resolve_credential",
                        staticmethod(lambda path, passphrase=None: (None, key)))
    v._embedder.model_sha256 = "0" * 64
    built = _encoders_built(monkeypatch)
    _written_with_a_vector(vault_path, key, 0)
    _call(server.memory_search, "which Postgres version does staging run")
    assert server._state["vault"] is not v
    assert server._state["vault"]._embedder is built[0] and len(built) == 1


def _live_texts(vault_path, key):
    after = Vault.unlock(vault_path, raw_key=key)
    return after, sorted(strip_provenance(r["text"]) for r in
                         after.recent("agent", limit=50)["results"])


def test_a_supersede_interrupted_by_another_write_is_stored_once(
        serving, vault_path, monkeypatch):
    """store(supersedes=[old]) journaled the replacement and its tombstone
    under two file locks. A write by another process between them failed the
    tombstone with the replacement already on disk, and _op's reopen and
    retry stored the replacement a second time."""
    v = serving
    old = v.store("The office printer is on the second floor",
                  caller="agent")["id"]
    key = v._master
    monkeypatch.setattr(Vault, "resolve_credential",
                        staticmethod(lambda path, passphrase=None: (None, key)))
    real_tombstone, fired = Vault._tombstone, []

    def tombstone(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            _written_by_another_process(self, vault_path)
        return real_tombstone(self, *args, **kwargs)
    monkeypatch.setattr(Vault, "_tombstone", tombstone)
    out = server._op(lambda h: h.store(
        "The office printer moved to the third floor", caller="agent",
        supersedes=[old]))
    assert fired
    # One append, record first and its tombstone after it.
    loaded = vaultfile.read_vault_file(vault_path)
    assert [e["op"] for e in vaultfile.decrypt_journal(
        loaded.header, loaded.journal_cts, key)] == ["store", "supersede"]
    after, texts = _live_texts(vault_path, key)
    assert texts.count("The office printer moved to the third floor") == 1
    assert after.get(old, caller="agent")["superseded_by"] == out["id"]


def test_store_many_refuses_a_fact_its_caller_may_not_write_before_storing_any(
        serving):
    """Validating the batch skipped the namespace grant, which Vault.store
    checks first, so the facts before one in a read-only namespace were
    stored and then the call failed."""
    with pytest.raises(server.MemoryToolError) as e:
        server.memory_store_many.__wrapped__(
            [{"text": "Alice leads the platform team"},
             {"text": "Bob leads the data team", "namespace": "packs/x"}],
            "from chat")
    assert "facts[1]" in str(e.value) and "read-only" in str(e.value)
    assert serving.db.count() == 0


def test_store_many_interrupted_by_another_write_stores_the_rest_once(
        serving, vault_path, monkeypatch):
    """A write by another process between two facts made _op rerun the whole
    batch. The first fact, already stored with its supersede applied, then
    failed validation as already superseded: the call reported a failure,
    the first fact stayed stored and the rest never were."""
    v = serving
    old = v.store("The office printer is on the second floor",
                  caller="agent")["id"]
    key = v._master
    monkeypatch.setattr(Vault, "resolve_credential",
                        staticmethod(lambda path, passphrase=None: (None, key)))
    real_store, fired = Vault.store, []

    def store(self, *args, **kwargs):
        out = real_store(self, *args, **kwargs)
        if not fired:
            fired.append(True)
            other = Vault.unlock(vault_path, raw_key=key)
            real_store(other, "Another agent stored this meanwhile",
                       caller="cli")
        return out
    monkeypatch.setattr(Vault, "store", store)
    out = json.loads(server.memory_store_many.__wrapped__(
        [{"text": "The office printer moved to the third floor",
          "supersedes": [old]},
         {"text": "Erin runs the Friday demo"}], "from chat"))
    assert out["count"] == 2
    after, texts = _live_texts(vault_path, key)
    assert texts == ["Another agent stored this meanwhile",
                     "Erin runs the Friday demo",
                     "The office printer moved to the third floor"]
    assert after.get(old, caller="agent")["superseded_by"] == \
        out["stored"][0]["id"]


def _through_the_sdk(monkeypatch, name, arguments):
    """A tool call as the MCP SDK makes one, from its tool manager down.

    The SDK's own wrapping decides what a failure says to the model: the
    text of the exception it raises here is the text of the isError result
    the client receives. Still no event loop: the body runs inline instead
    of on a worker thread, so the whole call is one step of the coroutine."""
    async def inline(fn, *args):
        return fn(*args)
    monkeypatch.setattr(server, "anyio", types.SimpleNamespace(
        to_thread=types.SimpleNamespace(run_sync=inline)))
    call = server.mcp.call_tool(name, arguments)
    try:
        call.send(None)
    except StopIteration as done:
        return done.value
    finally:
        call.close()
    raise AssertionError(f"{name} waited for an event loop")


def test_a_refused_tool_call_tells_the_model_why(serving, monkeypatch):
    """mcp 2.x passes a failure's text to the model only for its own
    ToolError. Anything else arrives as "Error executing tool <name>" and
    the reason stays in the server's log, and MemoryToolError was a
    RuntimeError, so the gate's refusal never reached the model that had
    to split the memory."""
    with pytest.raises(Exception) as e:
        _through_the_sdk(monkeypatch, "memory_store",
                         {"text": "x " * 150, "source": "from chat"})
    assert "characters refused" in str(e.value), str(e.value)
    assert serving.db.count() == 0


def test_a_locked_vault_tells_the_model_what_to_run(vault_path, monkeypatch):
    from compartment import vault as vault_module
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    Vault.create(vault_path, "CorrectHorse", creator="test").lock()
    for key, value in (("vault", None), ("path", vault_path),
                       ("caller", "agent")):
        monkeypatch.setitem(server._state, key, value)
    with pytest.raises(Exception) as e:
        _through_the_sdk(monkeypatch, "memory_search", {"query": "anything"})
    assert "compartment unlock" in str(e.value), str(e.value)


@pytest.fixture()
def own_state(monkeypatch):
    """A copy of server._state for this test, so that main() leaves nothing
    behind for the next one."""
    monkeypatch.setattr(server, "_state", dict(server._state))


def _serve(monkeypatch, vault_path, session, passphrase="CorrectHorse"):
    """server.main() with `session` in place of the stdio transport, its two
    background loops not started, and the passphrase in the environment as
    the only credential (no keychain lookup; None for no credential at
    all). main() handles SIGTERM and SIGHUP for the whole process, so the
    handlers it found are put back."""
    from compartment import vault as vault_module
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    monkeypatch.delenv("ENGRAM_PASSPHRASE", raising=False)
    if passphrase is None:
        monkeypatch.delenv("COMPARTMENT_PASSPHRASE", raising=False)
    else:
        monkeypatch.setenv("COMPARTMENT_PASSPHRASE", passphrase)
    monkeypatch.setattr(server, "_autolock_loop", lambda: None)
    monkeypatch.setattr(server, "_retag_loop", lambda: None)
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: session())
    handled = [getattr(signal, n) for n in ("SIGTERM", "SIGHUP")
               if hasattr(signal, n)]
    before = {sig: signal.getsignal(sig) for sig in handled}
    try:
        server.main(["--vault", vault_path, "--caller", "agent"])
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def _audited_on_disk(vault_path):
    v = Vault.unlock(vault_path, passphrase="CorrectHorse")
    return [r["op"] for r in v.db.conn.execute(
        "SELECT op FROM audit ORDER BY seq")]


def test_a_session_that_ends_saves_the_reads_it_audited(
        vault, vault_path, own_state, monkeypatch):
    """A read is audited in RAM and reaches disk only with a save, and the
    server saved only on auto-lock, a retag pass or memory_lock. A host
    that closed stdin before any of those took every search and get of the
    session with it, which SECURITY.md says only a kill -9 can do."""
    rid = vault.store("The staging database runs Postgres 16",
                      caller="test")["id"]
    vault.lock()

    def session():
        _call(server.memory_search, "which Postgres version does staging run")
        _call(server.memory_get, rid)
    _serve(monkeypatch, vault_path, session)
    assert _audited_on_disk(vault_path)[-2:] == ["search", "get"]


def test_a_server_stopped_with_sigterm_saves_them_too(
        vault, vault_path, own_state, monkeypatch):
    """SIGTERM, the other way a host ends a server, had no handler: the
    process died on the spot, with the same loss. It still ends by the
    signal, once the save is on disk."""
    vault.lock()
    before = signal.getsignal(signal.SIGTERM)
    ended = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: ended.append(
        (pid, sig, signal.getsignal(sig), _audited_on_disk(vault_path)[-1])))

    def session():
        _call(server.memory_search, "which Postgres version does staging run")
        handler = signal.getsignal(signal.SIGTERM)
        if handler is before or not callable(handler):
            pytest.fail("compartment serve leaves SIGTERM to end it on the "
                        "spot, with nothing saved")
        signal.raise_signal(signal.SIGTERM)
    _serve(monkeypatch, vault_path, session)
    assert ended == [(os.getpid(), signal.SIGTERM, signal.SIG_DFL, "search")]


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"),
                    reason="Windows has no SIGHUP")
def test_a_server_whose_terminal_closes_saves_them_too(
        vault, vault_path, own_state, monkeypatch):
    """A client running in a terminal starts its servers in its process
    group, and closing the terminal sends them all SIGHUP, whose default
    action ended the server with the session's audited reads unsaved."""
    vault.lock()
    ended = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: ended.append(
        (pid, sig, signal.getsignal(sig), _audited_on_disk(vault_path)[-1])))

    def session():
        _call(server.memory_search, "which Postgres version does staging run")
        if not callable(signal.getsignal(signal.SIGHUP)):
            pytest.fail("compartment serve leaves SIGHUP to end it on the "
                        "spot, with nothing saved")
        signal.raise_signal(signal.SIGHUP)
    _serve(monkeypatch, vault_path, session)
    assert ended == [(os.getpid(), signal.SIGHUP, signal.SIG_DFL, "search")]


def _set(vault_path, **settings):
    from compartment.acl import VaultConfig
    cfg = VaultConfig.load(vault_path)
    cfg.settings.update(settings)
    cfg.save(vault_path)


def test_a_server_started_locked_takes_its_settings_from_the_vault(
        vault, vault_path, own_state, monkeypatch):
    """main() read auto_lock_minutes and the retag settings only when the
    vault opened at startup, and a server starts locked after every
    restart: it kept the defaults, so a 5-minute auto-lock was 30 minutes
    and a retag pass the settings turned off ran every 6 hours."""
    from compartment import retag
    autolock, retag_loop = server._autolock_loop, server._retag_loop
    _set(vault_path, auto_lock_minutes=5, retag_interval_hours=0)
    key = vault._master
    vault.lock()

    def session():                     # `compartment unlock`, then a call
        assert server._state["vault"] is None          # it started locked
        monkeypatch.setattr(Vault, "resolve_credential", staticmethod(
            lambda path, passphrase=None: (None, key)))
        _call(server.memory_search, "anything")
    _serve(monkeypatch, vault_path, session, passphrase=None)
    v = server._state["vault"]
    passes = []
    monkeypatch.setattr(retag, "targets", lambda h: passes.append(1) or [])
    server._state["last_retag"] = 0
    _fake_sleeps(monkeypatch, passes=1)
    with pytest.raises(_Stop):
        retag_loop()
    assert passes == [], "the retag pass ran with retag_interval_hours 0"
    server._state["last_op"] = time.time() - 6 * 60
    _fake_sleeps(monkeypatch, passes=1)
    with pytest.raises(_Stop):
        autolock()
    assert v._locked, "six idle minutes did not lock a 5-minute auto-lock"


def test_a_running_server_takes_a_settings_change(serving, vault_path,
                                                  monkeypatch):
    """The panel's Auto-lock menu writes the config while servers run."""
    _set(vault_path, auto_lock_minutes=30)
    monkeypatch.setitem(server._state, "last_op", time.time() - 6 * 60)
    _run_autolock(monkeypatch, passes=1)
    assert not serving._locked
    _set(vault_path, auto_lock_minutes=5)
    _run_autolock(monkeypatch, passes=1)
    assert serving._locked


def test_a_config_cut_short_keeps_the_settings_in_use(serving, vault_path,
                                                      monkeypatch):
    """A config read in the middle of a write must not reset the settings
    or end a loop: the next look reads it again."""
    from compartment.acl import VaultConfig
    _set(vault_path, auto_lock_minutes=5)
    server._read_settings()
    assert server._state["auto_lock_min"] == 5
    whole = VaultConfig.load(vault_path)
    with open(vault_path + ".config.json", "w", encoding="utf-8") as f:
        f.write('{"settings": {"auto_lock_minutes"')
    server._read_settings()
    assert server._state["auto_lock_min"] == 5
    whole.settings["auto_lock_minutes"] = 15
    whole.save(vault_path)
    server._read_settings()
    assert server._state["auto_lock_min"] == 15


def _stored_credential_only(monkeypatch):
    """No keychain and no passphrase in the environment, so the session
    store is the only credential there is; every look is made at once."""
    from compartment import vault as vault_module
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "CREDENTIAL_CHECK_SECONDS", 0, raising=False)


def test_a_lock_reaches_a_running_server(serving, vault_path, monkeypatch):
    """`compartment lock`, which the panel's Lock runs, clears the stored
    credentials and touches nothing else. A running server looked for a
    credential only when its own handle was missing, locked or stale, so
    it kept the key and went on reading and writing memories until it had
    been idle for the whole auto-lock time."""
    from compartment import session
    _stored_credential_only(monkeypatch)
    session.store(vault_path, serving._master)        # `compartment unlock`
    try:
        _call(server.memory_search, "which Postgres version does staging run")
        session.clear(vault_path)                     # `compartment lock`
        with pytest.raises(server.MemoryToolError) as e:
            _call(server.memory_store, "Lock does not stop this", "from chat")
    finally:
        session.clear(vault_path)
    assert "compartment unlock" in str(e.value)
    assert serving._locked and serving._master is None
    _, texts = _live_texts(vault_path, Vault.unlock(
        vault_path, passphrase="CorrectHorse")._master)
    assert texts == []


def test_a_passphrase_in_the_environment_outlasts_a_lock(serving, vault_path,
                                                         monkeypatch):
    """A passphrase in the environment is standing intent (SECURITY.md):
    clearing the stored credentials leaves it, so the server keeps going."""
    from compartment import session
    _stored_credential_only(monkeypatch)
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", "CorrectHorse")
    session.store(vault_path, serving._master)
    try:
        _call(server.memory_search, "anything")
        session.clear(vault_path)
        out = json.loads(_call(server.memory_search, "anything"))
    finally:
        session.clear(vault_path)
    assert "results" in out and not serving._locked


def test_a_credential_that_cannot_be_read_leaves_the_server_working(
        serving, monkeypatch):
    """Could not tell is not locked: a credential file that failed to read
    (a permissions slip, a stalled share) must not lock a working server.
    The next look asks again."""
    from compartment import session
    from compartment.crypto import CryptoError
    _stored_credential_only(monkeypatch)
    monkeypatch.setattr(session, "watch_signature", lambda path: ("changed",))

    def unreadable(path, passphrase=None):
        raise CryptoError("Cannot read the session credential")
    monkeypatch.setattr(Vault, "find_credential", staticmethod(unreadable))
    out = json.loads(_call(server.memory_search, "anything"))
    assert "results" in out and not serving._locked


def test_the_credential_is_looked_for_at_most_every_few_seconds(
        serving, monkeypatch):
    from compartment import session
    looks, clock = [], [1000.0]
    monkeypatch.setattr(session, "watch_signature",
                        lambda path: looks.append(1) or ("unchanged",))
    monkeypatch.setattr(server, "time", types.SimpleNamespace(
        time=time.time, sleep=time.sleep, monotonic=lambda: clock[0]))
    for key, value in (("cred_sig", ("unchanged",)),
                       ("cred_checked", clock[0])):
        monkeypatch.setitem(server._state, key, value)
    for _ in range(3):
        server._vault()
    assert looks == []
    clock[0] += server.CREDENTIAL_CHECK_SECONDS
    server._vault()
    server._vault()
    assert looks == [1]


def test_a_vault_opened_by_memory_unlock_stays_open(vault, vault_path,
                                                    own_state, monkeypatch):
    """memory_unlock opens with the passphrase it is given and stores no
    credential, so the absence of one is not a lock there."""
    _stored_credential_only(monkeypatch)
    _set(vault_path, unlock_tool_enabled=True)
    vault.lock()
    for key, value in (("vault", None), ("path", vault_path),
                       ("caller", "agent")):
        monkeypatch.setitem(server._state, key, value)
    _call(server.memory_unlock, "CorrectHorse")
    out = json.loads(_call(server.memory_search, "anything"))
    assert "results" in out
