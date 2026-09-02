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
