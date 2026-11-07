"""Associative recall: 1-hop relation-graph spreading activation, bidirectional
camelCase/snake_case identifier matching, multi-query faceted search, and
cross-OS git project context priors.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from compartment import platforms, ranking as R, server
from compartment.embed_daemon import RemoteEmbedder
from compartment.vault import Vault

from conftest import PASS, statements_run, table_walks


class _NeutralEncoder:
    """Deterministic unit-vector encoder whose records all have exact cosine
    0.52 with every query while remaining mutually near-orthogonal across
    distinct texts (~0.27) so tests isolate FTS, identifier, and graph channels
    without triggering near-duplicate collapsing on `store()`."""
    dim = 384

    def __init__(self):
        v = np.zeros(self.dim, dtype=np.float32)
        v[0] = 1.0
        self._q = v
        self.batch_calls = 0

    def embed_record(self, text: str) -> np.ndarray:
        import hashlib
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "little"))
        tail = rng.standard_normal(self.dim - 1).astype(np.float32)
        tail /= float(np.linalg.norm(tail)) or 1.0
        r = np.empty(self.dim, dtype=np.float32)
        r[0] = 0.52
        r[1:] = float(np.sqrt(1.0 - 0.52 ** 2)) * tail
        return r.reshape(1, self.dim)

    def embed_query(self, _text: str) -> np.ndarray:
        return self._q.copy()

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        self.batch_calls += 1
        return np.repeat(self._q.reshape(1, self.dim), len(texts), axis=0)


def _neutral_vault(path: str) -> Vault:
    v = Vault.create(path, PASS, creator="test")
    v._embedder = _NeutralEncoder()
    return v


# ---------------------------------------- 1-hop relation-graph recall

def test_relation_graph_recalls_zero_lexical_overlap_memory_via_src_id_and_neighbor(
        tmp_path):
    v = _neutral_vault(str(tmp_path / "graph.vault"))
    # Distractors that share neither query words nor graph edges.
    for i in range(6):
        v.store(f"Unrelated facility note {i} about cafeteria menus.",
                caller="test", source="memo")

    # 1) Linked via src_id: text mentions neither "Helios" nor "deployment".
    runbook_id = v.store(
        "Cluster runbook lives in ops/eu4.md and rotates keys every Tuesday.",
        caller="test", source="ops")["id"]
    v.link("Project Helios", "deployed_on", "eu4-prod", caller="test",
           src_id=runbook_id)

    hits = v.search("Project Helios", caller="test")["results"]
    assert any(h["id"] == runbook_id for h in hits), hits

    # 2) Linked via 1-hop rare entity expansion (no src_id on the edge!):
    # query mentions "PaymentGateway", relation connects PaymentGateway ->
    # "Priya Ramanathan", and another memory mentions Priya Ramanathan.
    oncall_id = v.store(
        "Priya Ramanathan handles primary pager duty in UTC+5:30.",
        caller="test", source="roster")["id"]
    v.link("PaymentGateway", "maintained_by", "Priya Ramanathan",
           caller="test")

    hits2 = v.search("who maintains PaymentGateway", caller="test")["results"]
    assert any(h["id"] == oncall_id for h in hits2), hits2

    # 3) Co-linked via shared entity from a seed hit.
    seed_id = v.store(" MercuryQueue brokers webhook retries for billing.",
                      caller="test", source="arch")["id"]
    sibling_id = v.store("Dead-letter retention is set to fourteen days.",
                         caller="test", source="arch")["id"]
    v.link("MercuryQueue", "feeds", "billing-dlq", caller="test",
           src_id=seed_id)
    v.link("billing-dlq", "configured_by", "dlq-policy", caller="test",
           src_id=sibling_id)

    # Warm one-time namespace/recency caches after writes, then verify steady-state
    # search performs zero full-table scans on relations (only B-tree index probes).
    v.search("MercuryQueue", caller="test")
    with statements_run(v.db.conn) as ran:
        hits3 = v.search("MercuryQueue", caller="test")["results"]
    assert any(h["id"] == seed_id for h in hits3)
    assert any(h["id"] == sibling_id for h in hits3)
    # Seed hit (direct literal match) still ranks strictly ahead of 1-hop neighbor.
    ids3 = [h["id"] for h in hits3]
    assert ids3.index(seed_id) < ids3.index(sibling_id)
    rel_queries = [
        sql for sql in ran
        if sql.lstrip().upper().startswith("SELECT") and "relations" in sql
    ]
    assert rel_queries
    for sql in rel_queries:
        plan = [r[3] for r in v.db.conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert not any("SCAN" in d for d in plan), (sql, plan)


def test_relation_graph_respects_acl_namespaces_and_temporal_validity(tmp_path):
    v = _neutral_vault(str(tmp_path / "acl_graph.vault"))
    v.config.callers = {
        "admin": {"default_namespace": "main", "grants": {"*": "rw"}},
        "guest": {"default_namespace": "main",
                  "grants": {"main": "rw", "secret": "none"}},
    }

    pub_id = v.store(" Neutral operational note alpha.", caller="admin",
                     namespace="main", source="ops")["id"]
    # Secret relation pointing at a public record must NEVER fire for "guest".
    v.link("Codename Onyx", "uses_note", "alpha", caller="admin",
           namespace="secret", src_id=pub_id)

    guest_hits = v.search("Codename Onyx", caller="guest")["results"]
    assert not any(h["id"] == pub_id for h in guest_hits)
    admin_hits = v.search("Codename Onyx", caller="admin")["results"]
    assert any(h["id"] == pub_id for h in admin_hits)

    # Expired relation (valid_to in the past) does not boost or recall.
    exp_id = v.store(" Neutral operational note beta.", caller="admin",
                     namespace="main", source="ops")["id"]
    v.link("Legacy Cron", "ran_on", "old-box", caller="admin",
           namespace="main", src_id=exp_id, valid_to=time.time() - 3600)
    assert not any(
        h["id"] == exp_id
        for h in v.search("Legacy Cron", caller="admin")["results"]
    )


# ---------------------------------------- camelCase <-> snake_case identifiers

def test_bidirectional_camelcase_and_snake_case_recall_without_substring_leaks(
        tmp_path):
    # Tokenizer contract stays untouched, and substrings inside ordinary words
    # ("key" in "keyboard"/"keychain") still get zero coverage.
    assert R._tokens("maxMemoryChars") == ["maxmemorychars"]
    assert R.information_coverage(
        {"key": 5.0}, "my keyboard is loud; the keychain is unlocked"
    ) == 0.0

    # Bidirectional identifier coverage: camelCase <-> snake_case <-> words.
    assert R.information_coverage(
        {"maxmemorychars": 5.0},
        "Tune max_memory_chars in config.json."
    ) >= 0.90
    assert R.information_coverage(
        {"max": 3.0, "memory": 3.0, "chars": 3.0},
        "Tune maxMemoryChars in the client config."
    ) >= 0.90
    assert R.camel_subtokens("parseHTTPResponseCode") == [
        "parse", "http", "response", "code"
    ]

    v = _neutral_vault(str(tmp_path / "ident.vault"))
    snake_id = v.store(
        "Set opinion_reaffirm_threshold to 0.91 for strict opinion matching.",
        caller="test", source="code")["id"]
    camel_id = v.store(
        "Client SDK exposes fetchVaultSnapshot for read-only status checks.",
        caller="test", source="code")["id"]
    for i in range(5):
        v.store(f"General engineering handbook entry {i}.",
                caller="test", source="docs")

    # camelCase query -> snake_case memory
    r1 = v.search("opinionReaffirmThreshold", caller="test")["results"]
    assert r1 and r1[0]["id"] == snake_id

    # snake_case and space-separated queries -> camelCase memory
    r2 = v.search("fetch_vault_snapshot", caller="test")["results"]
    assert r2 and r2[0]["id"] == camel_id
    r3 = v.search("fetch vault snapshot", caller="test")["results"]
    assert r3 and r3[0]["id"] == camel_id


# ---------------------------------------- multi-query faceted search

def test_multi_query_faceted_search_batches_embeddings_and_fuses_results(
        tmp_path, monkeypatch):
    v = _neutral_vault(str(tmp_path / "facets.vault"))
    enc: _NeutralEncoder = v._embedder  # type: ignore[assignment]
    id_db = v.store(" Analytics warehouse runs ClickHouse on host ch-01.",
                    caller="test", source="infra")["id"]
    id_owner = v.store(" Nadia Kowalski owns the quarterly revenue pipeline.",
                       caller="test", source="org")["id"]
    for i in range(6):
        v.store(f"Filler note {i} about office plants.",
                caller="test", source="chat")

    enc.batch_calls = 0
    out = v.search(
        "ClickHouse warehouse",
        caller="test",
        queries=["ClickHouse warehouse", "Nadia Kowalski revenue pipeline"],
    )
    found_ids = {r["id"] for r in out["results"]}
    assert id_db in found_ids and id_owner in found_ids
    assert enc.batch_calls == 1   # all facets embedded in one batch call!

    # Also exposed on MCP memory_search without altering single-query behavior.
    monkeypatch.setattr(server, "_op", lambda fn: fn(v))
    mcp_res = json.loads(server.memory_search.__wrapped__(
        "ClickHouse warehouse",
        queries=["Nadia Kowalski revenue pipeline"],
    ))
    mcp_ids = {r["id"] for r in mcp_res["results"]}
    assert id_db in mcp_ids and id_owner in mcp_ids


def test_remote_embedder_embed_queries_falls_back_cleanly_on_older_daemon(
        monkeypatch):
    from pathlib import Path
    from compartment import embed_daemon
    monkeypatch.setattr(RemoteEmbedder, "_open", lambda self: None)
    rem = RemoteEmbedder(embed_daemon.DEFAULT_MODEL, Path("/tmp/unused.sock"),
                         spawn=False)
    calls = []

    def fake_call(op, **fields):
        calls.append(op)
        if op == "embed_queries":
            raise ValueError("unknown op 'embed_queries'")
        vec = np.zeros(rem.dim, dtype=np.float32)
        vec[0] = 1.0
        return {"ok": True, "vec": embed_daemon._pack(vec)}

    monkeypatch.setattr(rem, "_call", fake_call)
    mat = rem.embed_queries(["one", "two"])
    assert mat.shape == (2, rem.dim)
    assert calls == ["embed_queries", "embed_query", "embed_query"]


# ---------------------------------------- cross-OS git project context prior

@pytest.mark.parametrize("url,expected", [
    ("git@github.com:MaxFreedomPollard/Compartment.git",
     "maxfreedompollard/compartment"),
    ("https://github.com/MaxFreedomPollard/Compartment/",
     "maxfreedompollard/compartment"),
    ("https://oauth2:secret@gitlab.com/group/sub/service.git",
     "group/sub/service"),
    ("ssh://git@github.com:2222/AcmeCorp/RocketEngine.git",
     "acmecorp/rocketengine"),
    ("https://github.com", None),
    ("", None),
])
def test_normalize_git_remote_across_protocols(url, expected):
    assert platforms.normalize_git_remote(url) == expected


def test_detect_project_slug_handles_standard_git_worktrees_and_non_git(
        tmp_path):
    # Non-git directory -> None.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert platforms.detect_project_slug(plain) is None

    # Standard repo with origin remote, queried from a nested subdirectory.
    repo = tmp_path / "repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n'
        '\turl = git@github.com:AcmeOrg/BillingCore.git\n',
        encoding="utf-8",
    )
    assert platforms.detect_project_slug(sub) == "acmeorg/billingcore"

    # Git worktree where .git is a pointer file with commondir -> main .git.
    common = tmp_path / "main_git"
    wt_git = common / "worktrees" / "feature-x"
    wt_git.mkdir(parents=True)
    (common / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/AcmeOrg/WorktreeApp\n',
        encoding="utf-8",
    )
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt_dir = tmp_path / "wt_checkout"
    wt_dir.mkdir()
    (wt_dir / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert platforms.detect_project_slug(wt_dir) == "acmeorg/worktreeapp"


def test_active_project_prior_orders_current_repo_first_without_floor_inflation(
        tmp_path, monkeypatch):
    v = _neutral_vault(str(tmp_path / "proj.vault"))
    v.config.settings["duplicate_threshold"] = 2.0
    for i in range(5):
        v.store(f"General reference note {i} about team workflows.",
                caller="test", source="handbook", created=1_700_000_000.0)
    other_id = v.store(
        "Local development server listens on port 8080.",
        caller="test", source="readme", created=1_700_000_000.0,
        prov={"host": "h", "agent": "claude", "session": "-",
              "project": "acme/other-service"})["id"]
    active_id = v.store(
        "Local development server listens on port 8080.",
        caller="test", source="readme", created=1_700_000_000.0,
        prov={"host": "h", "agent": "claude", "session": "-",
              "project": "acme/current-service"})["id"]
    assert active_id != other_id

    # Irrelevant record in the active project must NOT cross RESULT_ABSOLUTE_FLOOR.
    noise_id = v.store(
        "Completely unrelated lunch preference about sourdough bread.",
        caller="test", source="chat", created=1_700_000_000.0,
        prov={"host": "h", "agent": "claude", "session": "-",
              "project": "acme/current-service"})["id"]

    monkeypatch.setattr("compartment.vault.detect_project_slug",
                        lambda: "acme/current-service")
    hits = v.search("development server port 8080", caller="test")["results"]
    hit_ids = [h["id"] for h in hits]
    assert hit_ids[:2] == [active_id, other_id]
    assert noise_id not in hit_ids

    # Switching to the other repo flips the tie-break cleanly!
    monkeypatch.setattr("compartment.vault.detect_project_slug",
                        lambda: "acme/other-service")
    hits_other = v.search("development server port 8080",
                          caller="test")["results"]
    assert [h["id"] for h in hits_other[:2]] == [other_id, active_id]
