"""The status panel reads the vault once, keeps it, and reads only what changed.

What it replaced: every click on the menu bar icon started `compartment
status` and then `compartment recent`, two fresh processes that each
decrypted the whole vault, and the second one resealed and rewrote the vault
file on its way out. A failed or slow read was shown as "locked". These tests
hold the replacement to the promises that fix all of that:

* the view never writes the vault, and never adds an audit row;
* it catches up on appended memories without decrypting the vault again;
* "locked" is shown only when no stored credential opens the vault;
* the panel asks the view only when a file it depends on changed;
* a click never waits: opening draws from what is already known.
"""
import io
import json
import os
import sys
import threading
import time

import pytest

from compartment import menubar, session, vaultfile
from compartment.crypto import CryptoError
from compartment.platforms import file_signature
from compartment.vault import Vault
from compartment.vaultview import VaultView, serve

from conftest import PASS


def _open(vault_path):
    """A fresh vault, unlocked the way `compartment unlock` leaves it."""
    v = Vault.create(vault_path, PASS, creator="test")
    session.store(vault_path, v._master)
    return v


def _audit_entries(vault_path):
    v = Vault.unlock(vault_path, passphrase=PASS)
    return v.db.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]


# --------------------------------------------------------------- the view

def test_a_look_never_writes_the_vault(vault_path):
    v = _open(vault_path)
    v.store("The panel view is read-only.", caller="t", source="test")
    v.save()
    audit_before = _audit_entries(vault_path)
    before = file_signature(vault_path)

    view = VaultView(vault_path)
    for _ in range(3):
        snap = view.snapshot()
        assert snap["ok"] and snap["locked"] is False
    assert snap["organic"] == 1
    assert [r["text"] for r in snap["recent"]][-1].startswith(
        "The panel view is read-only.")
    # the same file, byte for byte in size, same inode, same mtime
    assert file_signature(vault_path) == before
    # and nothing appended to the audit chain either
    assert _audit_entries(vault_path) == audit_before


def test_it_reads_only_the_memories_appended_since(vault_path, monkeypatch):
    """An agent storing a memory appends one journal entry. The view reads
    that entry alone - it does not decrypt the vault again."""
    v = _open(vault_path)
    v.save()
    view = VaultView(vault_path)
    first = view.snapshot()
    assert first["read"] == "full" and first["organic"] == 0

    again = view.snapshot()
    assert again["read"] == "cached"

    v.store("Stored by another process after the panel looked.",
            caller="t", source="test")               # a journal append
    later = view.snapshot()
    assert later["read"] == "tail"
    assert later["organic"] == 1
    assert later["recent"][-1]["text"].startswith("Stored by another process")

    # proof that it did not start over: the whole-vault decrypt is not run
    calls = []
    real = vaultfile.decrypt_payload

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    v.store("And one more.", caller="t", source="test")
    monkeypatch.setattr(vaultfile, "decrypt_payload", counting)
    assert view.snapshot()["organic"] == 2
    assert calls == []


def test_a_rewritten_file_is_read_again(vault_path):
    v = _open(vault_path)
    v.save()
    view = VaultView(vault_path)
    view.snapshot()
    v.store("Compacted into the payload below.", caller="t", source="test")
    v.save()                                  # a rewrite: new file, new nonce
    snap = view.snapshot()
    assert snap["read"] == "full" and snap["organic"] == 1


def test_it_catches_several_appends_and_a_torn_one(vault_path):
    """A partial entry at the end is an append still being written: it is
    skipped now and read once it is whole, never treated as damage."""
    v = _open(vault_path)
    v.save()
    view = VaultView(vault_path)
    view.snapshot()
    for i in range(3):
        v.store(f"Appended memory number {i}.", caller="t", source="test")
    with open(vault_path, "ab") as f:          # half a frame, as if mid-write
        f.write(b"\x00\x00\x01")
    snap = view.snapshot()
    assert snap["ok"] and snap["organic"] == 3


def test_locked_means_no_stored_credential(vault_path):
    _open(vault_path)
    view = VaultView(vault_path)
    assert view.snapshot()["locked"] is False and view.holding
    session.clear(vault_path)                  # `compartment lock`
    snap = view.snapshot()
    assert snap == {"ok": True, "exists": True, "locked": True}
    assert not view.holding                    # and the key is gone with it


def test_a_credential_that_does_not_open_it_is_reported_as_such(vault_path,
                                                                 tmp_path):
    """Locked, with the reason - the passphrase is the remedy, and the panel
    says so rather than showing an unexplained lock."""
    Vault.create(vault_path, PASS, creator="test")
    other = Vault.create(str(tmp_path / "other.vault"), PASS, creator="test")
    session.store(vault_path, other._master)   # someone else's key
    snap = VaultView(vault_path).snapshot()
    assert snap["locked"] is True and snap["credential_refused"] is True


def test_could_not_tell_is_not_locked(vault_path, monkeypatch):
    """An unreadable credential file, a boot that cannot be identified: the
    vault may be perfectly open. Saying "locked" here is the bug."""
    _open(vault_path)

    def cannot_tell(*a, **k):
        raise CryptoError("Cannot read the session credential")

    monkeypatch.setattr(Vault, "find_credential", staticmethod(cannot_tell))
    snap = VaultView(vault_path).snapshot()
    assert snap["ok"] is False and "locked" not in snap
    assert "could not check" in snap["error"]


def test_expired_memories_are_left_out_not_deleted(vault_path):
    v = _open(vault_path)
    v.store("A fact that stopped being true last year.", caller="t",
            source="test", expires="2020-01-01", _expiry_strict=False)
    v.store("A fact that is still true.", caller="t", source="test")
    v.save()
    before = file_signature(vault_path)
    view = VaultView(vault_path)
    snap = view.snapshot()
    assert snap["organic"] == 1
    assert "still true" in snap["recent"][-1]["text"]
    # not swept: the expired record is still held, and the file untouched
    held = view._v.db.conn.execute(
        "SELECT COUNT(*) c FROM records WHERE expires IS NOT NULL")
    assert held.fetchone()["c"] == 1
    assert file_signature(vault_path) == before


def test_it_lets_go_of_the_key_when_the_credential_goes(vault_path):
    """Between requests: a `compartment lock` typed in a terminal takes the
    key out of the view too, without waiting for the next click."""
    _open(vault_path)
    view = VaultView(vault_path)
    view.snapshot()
    view.check_credential()
    assert view.holding                        # nothing changed: keeps it
    session.clear(vault_path)
    view.check_credential()
    assert not view.holding


def test_no_vault_file(tmp_path):
    snap = VaultView(str(tmp_path / "missing.vault")).snapshot()
    assert snap == {"ok": True, "exists": False}


# ---------------------------------------------------------- the protocol

def test_serve_answers_each_request_by_id_and_ends_with_its_input(vault_path):
    _open(vault_path)
    lines = "\n".join([
        json.dumps({"op": "snapshot", "id": 7, "limit": 2}),
        "this is not json",
        json.dumps({"op": "nope", "id": 8}),
        json.dumps({"op": "ping", "id": 9}),
    ]) + "\n"
    out = io.StringIO()
    assert serve(vault_path, stdin=io.StringIO(lines), out=out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["id"] == 7 and replies[0]["locked"] is False
    assert replies[1] == {"ok": False,
                          "error": "request is not a JSON object"}
    assert replies[2]["ok"] is False and replies[2]["id"] == 8
    assert replies[3]["ok"] is True and replies[3]["holding"] is True
    # plain ASCII on the wire, whatever the memories contain
    assert out.getvalue().isascii()


def test_serve_leaves_when_idle(vault_path):
    _open(vault_path)
    r, w = os.pipe()
    stdin = os.fdopen(r, "r")
    started = time.monotonic()
    try:
        assert serve(vault_path, stdin=stdin, out=io.StringIO(),
                     idle=0.3, check_every=0.1) == 0
    finally:
        os.close(w)
        stdin.close()
    assert time.monotonic() - started < 5


# ------------------------------------------------------- the panel state

class _FakeView:
    """Stands in for the view process and counts what it is asked."""

    def __init__(self, answer=None):
        self.answer = answer or {"ok": True, "exists": True, "locked": False,
                                 "records": 10, "organic": 2, "recent": []}
        self.asked = 0
        self.held_during = []
        self._held = False

    def request(self, op, timeout=None, **fields):
        self.asked += 1
        if self._held:
            return {"ok": False, "held": True}
        return self.answer() if callable(self.answer) else self.answer

    def held(self):
        import contextlib
        view = self

        @contextlib.contextmanager
        def ctx():
            view._held = True
            try:
                yield
            finally:
                view._held = False
        return ctx()

    def close(self):
        pass


@pytest.fixture()
def quiet_agents(monkeypatch):
    """No real agent configs, and a count of how often they are read."""
    reads = []

    def status(vault):
        reads.append(1)
        return {"claude": False, "hermes": False, "openclaw": False}

    monkeypatch.setattr(menubar, "integration_status", status)
    return reads


def test_nothing_changed_means_nothing_is_read(vault_path, quiet_agents):
    _open(vault_path)
    view = _FakeView()
    ps = menubar.PanelState(vault_path, view=view)
    ps.refresh()
    assert view.asked == 1 and len(quiet_agents) == 1
    for _ in range(5):
        ps.refresh()
    assert view.asked == 1                     # no change on disk: no ask
    assert len(quiet_agents) == 1              # and no agent config re-read


def test_a_change_to_the_vault_file_is_noticed(vault_path, quiet_agents):
    v = _open(vault_path)
    view = _FakeView()
    ps = menubar.PanelState(vault_path, view=view)
    ps.refresh()
    v.store("Something new.", caller="t", source="test")   # appends
    ps.refresh()
    assert view.asked == 2


def test_a_lock_or_unlock_anywhere_is_noticed(vault_path, quiet_agents):
    _open(vault_path)
    view = _FakeView()
    ps = menubar.PanelState(vault_path, view=view)
    ps.refresh()
    session.clear(vault_path)                  # `compartment lock`
    ps.refresh()
    assert view.asked == 2


def test_agent_configs_are_reread_only_when_they_change(vault_path, tmp_path,
                                                        quiet_agents,
                                                        monkeypatch):
    _open(vault_path)
    cfg = tmp_path / "home" / ".claude.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(menubar, "_claude_code_config", lambda: cfg)
    ps = menubar.PanelState(vault_path, view=_FakeView())
    ps.refresh()
    ps.refresh()
    assert len(quiet_agents) == 1
    cfg.write_text('{"mcpServers": {"compartment": {}}}', encoding="utf-8")
    ps.refresh()
    assert len(quiet_agents) == 2


def test_a_failed_read_keeps_the_last_known_state(vault_path, quiet_agents):
    """The complaint this fixes: clicking the menu bar seemed to lock the
    vault. A read that failed or timed out used to be shown as locked."""
    v = _open(vault_path)
    answers = iter([
        {"ok": True, "exists": True, "locked": False, "records": 5,
         "organic": 1, "recent": []},
        None,                                   # the view did not answer
    ])
    ps = menubar.PanelState(vault_path, view=_FakeView(lambda: next(answers)))
    assert ps.refresh()["locked"] is False
    v.store("Forces the next look to ask.", caller="t", source="test")
    st = ps.refresh()
    assert st["locked"] is False               # still what it last knew
    assert st["records"] == 5
    assert "Could not refresh" in st["error"]


def test_a_failed_first_read_is_unknown_not_locked(vault_path, quiet_agents):
    _open(vault_path)
    ps = menubar.PanelState(vault_path, view=_FakeView(lambda: None))
    st = ps.refresh()
    assert st["locked"] is None
    assert menubar.lock_badge(st) == "unknown"
    assert "locked" not in menubar.summarise(st)
    # and it asks again next time rather than trusting a read it never got
    ps.refresh()
    assert ps.view.asked == 2


def test_before_the_first_read_it_says_so(vault_path, quiet_agents):
    _open(vault_path)
    ps = menubar.PanelState(vault_path, view=_FakeView())
    st = ps.snapshot()                         # what a first click draws
    assert st["locked"] is None and st["checking"] is True
    assert menubar.lock_badge(st) == "checking…"
    assert menubar.summarise(st) == "reading the vault…"


def test_a_click_during_a_read_wins(vault_path, quiet_agents):
    """What the user just did is on screen; a read that started before it
    must not paint over it with an older answer."""
    _open(vault_path)
    ps = None

    def answer():
        if view.asked == 1:
            ps.mark(locked=True)              # a Lock click, mid read
            return {"ok": True, "exists": True, "locked": False,
                    "records": 1, "organic": 0, "recent": []}
        return {"ok": True, "exists": True, "locked": True}

    view = _FakeView(answer)
    ps = menubar.PanelState(vault_path, view=view)
    st = ps.refresh()
    assert st["locked"] is True                # the click, not the stale read
    assert view.asked == 2                     # and it read again after it


def test_lock_runs_with_no_view_holding_the_key(vault_path, quiet_agents):
    _open(vault_path)
    view = _FakeView()
    ps = menubar.PanelState(vault_path, view=view)
    ps.refresh()
    seen = []

    def lock_fn():
        seen.append(view._held)
        session.clear(vault_path)
        return True

    assert ps.lock(lock_fn) is True
    assert seen == [True]
    st = ps.snapshot()
    assert st["locked"] is True and st["recent"] == []


def test_refresh_async_reruns_once_for_requests_made_meanwhile(vault_path,
                                                                quiet_agents):
    _open(vault_path)
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return {"ok": True, "exists": True, "locked": False, "records": 3,
                "organic": 0, "recent": []}

    view = _FakeView(slow)
    landed = []
    ps = menubar.PanelState(vault_path, view=view,
                            on_change=lambda: landed.append(1))
    ps.refresh_async()
    for _ in range(4):
        ps.refresh_async(force=True)          # asked again while running
    gate.set()
    deadline = time.monotonic() + 10
    while ps._thread is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(landed) == 2                    # the run, then ONE rerun
    assert view.asked == 2


def test_fetch_state_is_one_process_that_writes_nothing(vault_path):
    """End to end, through the real `vaultview --once`."""
    v = _open(vault_path)
    v.store("Read through the real one-shot view.", caller="t", source="test")
    v.save()
    before = file_signature(vault_path)
    st = menubar.fetch_state(vault_path)
    assert st["locked"] is False and st["organic"] == 1
    assert st["recent"][0]["text"].startswith("Read through the real")
    assert file_signature(vault_path) == before


# ------------------------------------------------------- the view process

def test_the_real_view_process_end_to_end(vault_path):
    """A real child process: opened once, asked twice, reads the append,
    and gone when the panel closes."""
    v = _open(vault_path)
    v.save()
    ps = menubar.PanelState(vault_path)
    try:
        assert ps.refresh()["locked"] is False
        proc = ps.view._proc
        assert proc is not None and proc.poll() is None
        v.store("Arrived while the panel was open.", caller="t",
                source="test")
        st = ps.refresh()
        assert st["organic"] == 1
        assert ps.view._proc is proc           # the same process, still open
        assert ps.view.request("ping")["holding"] is True
    finally:
        ps.close()
    assert proc.poll() is not None             # and it is gone


def test_a_view_that_left_is_started_again(vault_path):
    _open(vault_path)
    view = menubar.ViewProcess(vault_path)
    try:
        assert view.request("ping")["ok"] is True
        first = view._proc
        first.kill()                           # as if it left, idle
        first.wait(5)
        assert view.request("ping")["ok"] is True
        assert view._proc is not first
    finally:
        view.close()


def test_held_means_no_view_process_at_all(vault_path):
    _open(vault_path)
    view = menubar.ViewProcess(vault_path)
    try:
        view.request("ping")
        proc = view._proc
        with view.held():
            assert proc.poll() is not None     # ended at once
            assert view.request("ping") == {"ok": False, "held": True}
            assert view._proc is None          # and none started
        assert view.request("ping")["ok"] is True
    finally:
        view.close()


def test_a_wedged_view_is_abandoned_not_waited_on(vault_path, monkeypatch):
    monkeypatch.setattr(menubar, "_view_argv", lambda v: [
        sys.executable, "-c", "import time; time.sleep(60)"])
    view = menubar.ViewProcess(vault_path)
    started = time.monotonic()
    assert view.request("ping", timeout=0.5) is None
    assert time.monotonic() - started < 10
    assert view._proc is None
    view.close()


def test_stray_output_is_not_mistaken_for_an_answer(vault_path, monkeypatch):
    script = ("import sys, json\n"
              "for line in sys.stdin:\n"
              "    req = json.loads(line)\n"
              "    print('a library warning on stdout', flush=True)\n"
              "    print(json.dumps({'id': req['id'] + 1000}), flush=True)\n"
              "    print(json.dumps({'id': req['id'], 'ok': True}), "
              "flush=True)\n")
    monkeypatch.setattr(menubar, "_view_argv",
                        lambda v: [sys.executable, "-c", script])
    view = menubar.ViewProcess(vault_path)
    try:
        assert view.request("ping", timeout=10) == {"id": 1, "ok": True}
    finally:
        view.close()


# ------------------------------------------------ found in review, now held

def test_drop_forgets_a_key_derived_from_a_passphrase(vault_path,
                                                      monkeypatch):
    """With the passphrase in the environment the view caches the key it
    derived, to spare Argon2 on every reload. Letting go of the vault must
    let go of that too."""
    v = Vault.create(vault_path, PASS, creator="test")
    v.save()
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)
    view = VaultView(vault_path)
    assert view.snapshot()["locked"] is False
    assert view._unwrapped is not None
    v.rekey("an entirely new passphrase")       # rewrites the file
    snap = view.snapshot()
    assert snap["locked"] is True and snap["credential_refused"] is True
    assert view._unwrapped is None and view._key is None


def test_a_file_that_cannot_be_looked_at_is_not_a_missing_vault(vault_path,
                                                                 monkeypatch):
    _open(vault_path)
    import compartment.vaultview as vv
    real_stat = os.stat

    def refusing(path, *a, **k):
        if str(path) == vault_path:
            raise PermissionError(13, "Permission denied", path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(vv.os, "stat", refusing)
    snap = VaultView(vault_path).snapshot()
    assert snap["ok"] is False and snap["exists"] is True
    monkeypatch.setattr(menubar.os, "stat", refusing)
    assert menubar.vault_presence(vault_path) is None
    ps = menubar.PanelState(vault_path, view=_FakeView(lambda: None))
    assert ps.snapshot()["exists"] is True        # never "not set up"
    st = ps.refresh()
    assert st["exists"] is True and st["locked"] is None
    assert ps.view.asked == 1                      # asked, not assumed missing


def test_lock_stays_within_reach_while_the_state_is_unknown():
    from compartment import systray
    base = {"exists": True, "records": 0, "organic": 0, "recent": [],
            "error": None, "integrations": {},
            "settings": dict(menubar.DEFAULT_SETTINGS)}
    unknown = systray.panel_rows({**base, "locked": None, "checking": False})
    assert ("lock", "Lock") in unknown
    assert ("unlock", "Unlock") not in unknown
    assert ("change", "Change password") not in unknown
    locked = systray.panel_rows({**base, "locked": True})
    assert ("unlock", "Unlock") in locked and ("lock", "Lock") not in locked
