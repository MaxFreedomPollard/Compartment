"""The dashboard: RAM-served, loopback-only, token-gated, read-only."""
import json
import os
import threading
import urllib.request

import pytest

from compartment import dash

# `compartment dash` intentionally binds a 127.0.0.1 socket and is deliberately
# NOT run under the offline guard (cmd_dash refuses if it is active). So skip
# these under COMPARTMENT_ASSERT_OFFLINE=1 - the guard blocks the loopback socket
# by design; the dashboard is exempt from the zero-network claim. They still
# run in every normal (non-guarded) test invocation.
pytestmark = pytest.mark.skipif(
    os.environ.get("COMPARTMENT_ASSERT_OFFLINE") == "1",
    reason="compartment dash binds a loopback socket; exempt from the offline guard")


@pytest.fixture()
def served(vault, vault_path):
    vault.store("Max prefers dark mode interfaces", caller="test",
                importance=0.8, tags=["personal"])
    vault.store("The office scanner lives at 10.0.0.7", caller="test",
                importance=0.75)
    vault.link("Max", "works at", "Outreach", caller="test")
    vault.save()
    ref = dash._VaultRef(vault_path, vault)
    httpd, url = dash.start(ref)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield url
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read(), dict(r.headers)


def test_page_and_stats(served):
    status, body, headers = _get(served)
    assert status == 200 and b"Compartment" in body and b"<canvas" in body
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    # the importance tiers rank recall; they are not a panel on the page
    assert b"What the vault remembers" not in body

    status, body, _ = _get(served + "api/stats")
    s = json.loads(body)
    assert s["records"] == 2 and s["relations"] == 1 and s["entities"] == 2
    assert "types" not in s
    assert s["audit_ok"] is True and s["growth"]


def test_graph_recent_search(served):
    _, body, _ = _get(served + "api/graph")
    g = json.loads(body)
    assert {n["label"] for n in g["nodes"]} == {"Max", "Outreach"}
    assert g["edges"] == [{"s": "max", "o": "outreach", "p": "works at"}]
    assert g["total_entities"] == 2 and g["total_relations"] == 1

    _, body, _ = _get(served + "api/recent")
    texts = [m["text"] for m in json.loads(body)["recent"]]
    assert any("dark mode" in t for t in texts)

    _, body, _ = _get(served + "api/search?q=dark%20mode%20preference")
    res = json.loads(body)["results"]
    assert res and "dark mode" in res[0]["text"]


def test_wrong_token_is_404_everywhere(served):
    base = served.rsplit("/", 2)[0]           # http://127.0.0.1:port
    for path in ("/", "/nope/", "/nope/api/stats"):
        req = urllib.request.Request(base + path)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 404


def test_dashboard_is_read_only(served):
    req = urllib.request.Request(served + "api/stats", data=b"x=1")  # POST
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 501                # no POST handler exists at all


def test_the_panel_hears_where_dash_is_serving(vault, vault_path, tmp_path,
                                               monkeypatch):
    """The panel's Dashboard button waits for the first line `compartment
    dash` prints, the one that says where it is serving. It went into the
    pipe unflushed, so it sat in the buffer for as long as dash served, and
    the button, and the app with it, never came back."""
    import sys
    from compartment import menubar, session
    session.store(vault_path, vault._master)          # `compartment unlock`
    # dash opens the browser itself; this one opens nothing.
    if sys.platform == "win32":
        browser = tmp_path / "browser.bat"
        browser.write_text("@exit /b 0\r\n", encoding="utf-8")
    else:
        browser = tmp_path / "browser"
        browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        browser.chmod(0o755)
    monkeypatch.setenv("BROWSER", str(browser))
    menubar._DASH.update(proc=None, url=None)
    out = {}
    clicked = threading.Thread(target=lambda: out.update(
        url=menubar.open_dashboard(vault_path)), daemon=True)
    clicked.start()
    clicked.join(timeout=30)
    try:
        assert not clicked.is_alive(), \
            "the panel was still waiting for dash's first line"
        url = out["url"]
        assert url and url.startswith("http://127.0.0.1:")
        status, _body, _ = _get(url + "api/stats")    # it serves that URL
        assert status == 200
    finally:
        menubar.stop_dashboard()
        clicked.join(timeout=15)
        session.clear(vault_path)


def _stored_credential_only(monkeypatch):
    """No keychain and no passphrase in the environment, so the session
    store is the only credential there is; every look is made at once."""
    from compartment import vault as vault_module
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dash, "CREDENTIAL_CHECK_SECONDS", 0, raising=False)


def test_a_lock_reaches_a_running_dashboard(served, vault, vault_path,
                                            monkeypatch):
    """`compartment lock`, which the panel's Lock runs, clears the stored
    credentials and touches nothing else. A running dashboard kept its own
    unlocked vault and never looked again, so its page went on answering
    with decrypted memories after the lock, until the process was stopped."""
    from compartment import session
    from compartment.platforms import file_signature
    _stored_credential_only(monkeypatch)
    session.store(vault_path, vault._master)          # `compartment unlock`
    try:
        _, body, _ = _get(served + "api/recent")
        assert json.loads(body)["recent"]
        before = file_signature(vault_path)
        session.clear(vault_path)                     # `compartment lock`
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(served + "api/recent")
    finally:
        session.clear(vault_path)
    assert e.value.code == 423                        # the page shows locked
    assert vault._locked and vault._master is None    # and the key is gone
    assert file_signature(vault_path) == before       # nothing was written
    status, body, _ = _get(served)                    # the page still renders
    assert status == 200 and b"Compartment" in body


def test_a_credential_that_cannot_be_read_leaves_the_dashboard_serving(
        served, vault, vault_path, monkeypatch):
    """Could not tell is not locked: a credential file that failed to read,
    or a boot that cannot be identified, says nothing about a lock."""
    from compartment import session
    from compartment.crypto import CryptoError
    from compartment.vault import Vault
    _stored_credential_only(monkeypatch)
    monkeypatch.setattr(session, "watch_signature", lambda path: object())

    def unreadable(path, passphrase=None):
        raise CryptoError("the session file could not be read")
    monkeypatch.setattr(Vault, "find_credential", staticmethod(unreadable))
    status, body, _ = _get(served + "api/recent")
    assert status == 200 and json.loads(body)["recent"]
    assert not vault._locked
