"""The store gate (shape enforced at the door) and the opinion kind:
update-first storage, supersede tombstones, replayable journal ops, the
recency-heavy opinion prior, and the two curation passes."""
import json
import time

import pytest

from compartment import curate, gate
from compartment import ranking as R
from compartment.crypto import CryptoError
from compartment.vault import Vault

from conftest import PASS


# ------------------------------------------------------------------ the gate

def test_length_cap_refuses_with_the_fix(vault):
    with pytest.raises(gate.MemoryShapeError) as e:
        vault.store("word " * 60, caller="test")
    assert "memory_store_many" in str(e.value)
    assert "200" in str(e.value)
    assert vault.db.count() == 0
    r = vault.store("x" * 200, caller="test")
    assert not r["duplicate"] and vault.db.count() == 1


def test_shape_lint_refuses_lists_headings_bold(vault):
    for text in ("first fact\n\nsecond fact",
                 "- item one\n- item two",
                 "## Findings",
                 "this is **important** to know"):
        with pytest.raises(gate.MemoryShapeError):
            vault.store(text, caller="test")
    assert vault.db.count() == 0


def test_stored_elsewhere_refused_but_paths_pass(vault):
    with pytest.raises(gate.MemoryShapeError) as e:
        vault.store("The retry fix was documented in the changelog.",
                    caller="test")
    assert "written down elsewhere" in str(e.value)
    r = vault.store("The vault file is stored in ~/.compartment/memory.vault",
                    caller="test")
    assert not r["duplicate"]


def test_narration_warns_but_stores(vault):
    r = vault.store("The user told me to always use tabs in Makefiles",
                    caller="test")
    assert "warning" in r and "metadata" in r["warning"]
    assert vault.db.count() == 1


def test_cap_reads_the_claim_not_the_stamp(vault):
    text = "y" * 195
    r = vault.store(text, caller="test", source="web search")
    stored = vault.get(r["id"], caller="test")["text"]
    assert len(stored) > 200 and "web search" in stored   # stamp appended


def test_gate_bypass_for_restores(vault):
    line = json.dumps({"text": "z " * 300, "namespace": "main"})
    assert vault.import_jsonl(line, caller="test") == 1
    r = vault.store("w " * 300, caller="test", _gate=False)
    assert not r["duplicate"]


def test_max_memory_chars_zero_disables_length(vault):
    vault.config.settings["max_memory_chars"] = 0
    r = vault.store("long " * 100, caller="test")
    assert not r["duplicate"]
    # the stored-elsewhere check is about content, not length, and stays on
    with pytest.raises(gate.MemoryShapeError):
        vault.store("Everything was recorded in the master guide.",
                    caller="test")


# ------------------------------------------------------------- opinion kind

def test_kind_defaults_and_roundtrip(vault):
    f = vault.store("The office door code changed to 4821", caller="test")
    o = vault.store("Max prefers tabs over spaces in Makefiles",
                    caller="test", kind="opinion")
    assert f["kind"] == "fact" and o["kind"] == "opinion"
    assert vault.get(o["id"], caller="test")["kind"] == "opinion"
    hits = {r["id"]: r for r in
            vault.search("tabs or spaces", caller="test")["results"]}
    assert hits[o["id"]]["kind"] == "opinion"
    recent = {r["id"]: r for r in
              vault.recent(caller="test", limit=10)["results"]}
    assert recent[f["id"]]["kind"] == "fact"
    with pytest.raises(CryptoError):
        vault.store("valid text", caller="test", kind="belief")


def test_restating_an_opinion_reaffirms(vault):
    a = vault.store("Max prefers dark roast coffee", caller="test",
                    kind="opinion")
    b = vault.store("Max prefers dark roast coffee", caller="test",
                    kind="opinion")
    assert b["reaffirmed"] and b["id"] == a["id"]
    assert vault.db.count() == 1
    got = vault.get(a["id"], caller="test")
    assert got["affirmed_local"]


def test_conflicting_opinion_refused_then_superseded(vault):
    vault.config.settings["opinion_update_threshold"] = 0.30
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    a = vault.store("Max prefers Zoho for the custom-domain mail",
                    caller="test", kind="opinion")
    res = vault.store("Max prefers Fastmail for the custom-domain mail",
                      caller="test", kind="opinion")
    assert res["stored"] is False
    assert res["conflicts"][0]["id"] == a["id"]
    assert "Zoho" in res["conflicts"][0]["text"]
    assert "supersedes" in res["action"]
    assert vault.db.count() == 1                      # nothing inserted
    b = vault.store("Max prefers Fastmail for the custom-domain mail",
                    caller="test", kind="opinion", supersedes=[a["id"]])
    assert b["superseded"] == [a["id"]]
    ids = [r["id"] for r in
           vault.search("which mail provider does Max prefer",
                        caller="test")["results"]]
    assert b["id"] in ids and a["id"] not in ids
    assert a["id"] not in [r["id"] for r in
                           vault.recent(caller="test", limit=10)["results"]]
    old = vault.get(a["id"], caller="test")
    assert old["superseded_by"] == b["id"] and "SUPERSEDED" in old["note"]
    assert vault.status()["superseded_records"] == 1


def test_supersedes_empty_list_keeps_both(vault):
    vault.config.settings["opinion_update_threshold"] = 0.30
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    a = vault.store("Max prefers Vim keybindings in the editor",
                    caller="test", kind="opinion")
    b = vault.store("Max prefers Emacs keybindings in the terminal",
                    caller="test", kind="opinion", supersedes=[])
    assert b.get("stored") is not False
    assert any(s["id"] == a["id"] for s in b["similar_live_opinions"])
    assert vault.db.count() == 2


def test_supersede_corrects_a_fact(vault):
    a = vault.store("The deploy script lives at ~/bin/deploy.sh",
                    caller="test")
    b = vault.store("The deploy script lives at ~/bin/deploy-v2.sh",
                    caller="test", supersedes=[a["id"]])
    assert not b["duplicate"]                 # dedup skipped on supersede
    ids = [r["id"] for r in
           vault.search("where is the deploy script", caller="test")["results"]]
    assert b["id"] in ids and a["id"] not in ids


def test_supersede_validation(vault):
    a = vault.store("A fact that will be replaced", caller="test")
    b = vault.store("The fact that replaced it", caller="test",
                    supersedes=[a["id"]])
    with pytest.raises(CryptoError):
        vault.store("no such target", caller="test", supersedes=["missing"])
    with pytest.raises(CryptoError):                  # already superseded
        vault.store("replacing history", caller="test", supersedes=[a["id"]])
    with pytest.raises(CryptoError):                  # self-supersede
        vault.supersede(b["id"], b["id"], caller="test")


def test_reaffirm_and_supersede_replay_from_journal(vault, vault_path):
    vault.config.settings["opinion_update_threshold"] = 0.30
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    a = vault.store("Max prefers rsync for the NAS backups", caller="test",
                    kind="opinion")
    vault.config.settings["opinion_reaffirm_threshold"] = 0.97
    vault.store("Max prefers rsync for the NAS backups", caller="test",
                kind="opinion")                       # journals a reaffirm
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    b = vault.store("Max prefers restic for the NAS backups", caller="test",
                    kind="opinion", supersedes=[a["id"]])
    del vault                                         # journal only, no save
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    old = v2.get(a["id"], caller="test")
    assert old["superseded_by"] == b["id"] and old["affirmed_local"]
    ids = [r["id"] for r in
           v2.search("NAS backup tool preference", caller="test")["results"]]
    assert b["id"] in ids and a["id"] not in ids      # index rebuilt clean


def test_export_carries_history_and_import_skips_it(vault, tmp_path):
    a = vault.store("The API tier in use is the free one", caller="test")
    vault.store("The API tier in use is the paid one", caller="test",
                supersedes=[a["id"]])
    dump = vault.export_jsonl()
    rows = [json.loads(l) for l in dump.splitlines()]
    assert any(r["superseded_by"] for r in rows)
    v2 = Vault.create(str(tmp_path / "fresh.vault"), PASS, creator="test")
    assert v2.import_jsonl(dump, caller="test") == 1  # live record only
    assert v2.db.count() == 1


# ----------------------------------------------------------------- ranking

def test_opinion_prior_is_recency_heavy():
    now = time.time()
    old = now - 90 * 86400
    # a re-affirmed opinion outranks its own age
    assert R.prior(0.5, old, now=now, kind="opinion", affirmed=now) > \
        R.prior(0.5, old, now=now)
    # an unaffirmed old opinion decays harder than an old fact
    assert R.prior(0.5, old, now=now, kind="opinion") < \
        R.prior(0.5, old, now=now)
    # defaults unchanged: the fact prior is what it always was
    assert R.prior(0.5, now, now=now) == pytest.approx(R.W_RECENCY)


# ---------------------------------------------------------------- curation

def test_atomize_lists_splits_and_supersedes(vault):
    blob = ("The Vault-Publishing folder moved to Desktop. " * 8).strip()
    old = vault.store(blob, caller="test", source="from chat",
                      discovered="2026-07-20", created=1000000.0,
                      _gate=False)
    listed = curate.oversized(vault)
    assert [e["id"] for e in listed] == [old["id"]]
    assert listed[0]["discovered"] == "2026-07-20"
    plan = [{"id": old["id"], "pieces": [
        {"text": "Vault-Publishing moved off TOT scope to ~/Desktop/Publishing"},
        {"text": "Vault-Publishing holds 242 files, 634 MB, count+byte verified"},
    ]}]
    out = curate.apply_plan(vault, plan, caller="test")
    assert out == {"blobs_superseded": 1, "pieces_stored": 2,
                   "duplicate_pieces_skipped": 0}
    assert curate.oversized(vault) == []              # nothing left over
    hits = vault.search("where did Vault-Publishing move",
                        caller="test")["results"]
    assert hits and hits[0]["id"] != old["id"]
    piece = vault.get(hits[0]["id"], caller="test")
    assert piece["created"] == 1000000.0              # the blob's own date
    assert piece["discovered"] == "2026-07-20"
    assert vault.get(old["id"], caller="test")["superseded_by"]


def test_atomize_validates_the_whole_plan_first(vault):
    old = vault.store("blob " * 100, caller="test", _gate=False)
    bad = [{"id": old["id"], "pieces": [{"text": "fine piece"},
                                        {"text": "far too long " * 30}]}]
    with pytest.raises(CryptoError):
        curate.apply_plan(vault, bad, caller="test")
    assert vault.db.count() == 1                      # nothing was written
    assert not vault.get(old["id"], caller="test").get("superseded_by")


def test_opinions_audit_backfills_clusters_and_keeps_newest(vault):
    vault.config.settings["opinion_update_threshold"] = 0.30
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    a = vault.store("Max prefers Zoho for the custom-domain mail",
                    caller="test", kind="opinion", created=1000000.0)
    b = vault.store("Max prefers Fastmail for the custom-domain mail",
                    caller="test", kind="opinion", supersedes=[])
    legacy = vault.store("Max prefers window seats on long flights",
                         caller="test", created=2000000.0, _gate=False)
    vault.db.set_kind(legacy["id"], None)             # simulate an old record
    changed = curate.backfill_kinds(vault, caller="test")
    assert [c["id"] for c in changed] == [legacy["id"]]
    assert vault.get(legacy["id"], caller="test")["kind"] == "opinion"
    clusters = curate.opinion_clusters(vault, threshold=0.30)
    mail = next(c for c in clusters
                if {e["id"] for e in c} >= {a["id"], b["id"]})
    assert mail[0]["id"] == b["id"]                   # newest first
    actions = curate.keep_newest(vault, [mail], caller="test")
    assert {"superseded": a["id"], "by": b["id"]} in actions
    assert vault.get(a["id"], caller="test")["superseded_by"] == b["id"]


# ----------------------------------------------- fixes from the review pass

def test_gate_spares_code_and_place_claims(vault):
    """Globs, exponents, code fragments and app/folder locations are single
    legitimate claims and must pass; real formatting still bounces."""
    for text in ("The build globs src/**/*.py for test discovery",
                 "In Python 2**10 is 1024, used as the chunk size",
                 "# noqa: E501 disables flake8's line-length check",
                 "The AWS token is saved in the notes app on the phone",
                 "Screenshots are written to the docs folder in the repo"):
        assert not vault.store(text, caller="test").get("duplicate"), text
    for text in ("this is **important** to know",
                 "## Findings",
                 "Everything was recorded in the master guide."):
        with pytest.raises(gate.MemoryShapeError):
            vault.store(text, caller="test")


def test_rejection_hint_is_per_surface():
    hermes = gate.rejection("x " * 200,
                            many_hint="call compartment_store once per claim")
    assert "compartment_store once per claim" in hermes
    assert "memory_store_many" not in hermes
    assert "memory_store_many" in gate.rejection("x " * 200)


def test_opinions_deduplicate_on_gate_bypass_paths(vault):
    a = vault.store("Max prefers dark roast coffee in the morning",
                    caller="test", kind="opinion", _gate=False)
    b = vault.store("Max prefers dark roast coffee in the morning",
                    caller="test", kind="opinion", _gate=False)
    assert b["duplicate"] and b["id"] == a["id"]
    assert vault.db.count() == 1


def test_supersedes_empty_list_keeps_the_duplicate_guard(vault):
    a = vault.store("The deploy script lives at ~/bin/deploy.sh",
                    caller="test")
    b = vault.store("The deploy script lives at ~/bin/deploy.sh",
                    caller="test", supersedes=[])
    assert b["duplicate"] and b["id"] == a["id"]


def test_expired_opinion_neither_blocks_nor_leaks(vault):
    vault.config.settings["opinion_update_threshold"] = 0.30
    a = vault.store("Max prefers Zoho for the custom-domain mail",
                    caller="test", kind="opinion", expires="2d")
    vault.db.conn.execute(
        "UPDATE records SET expires = '2020-01-01' WHERE id = ?", (a["id"],))
    res = vault.store("Max prefers Fastmail for the custom-domain mail",
                      caller="test", kind="opinion")
    assert res.get("stored") is not False       # no conflict with the expired


def test_conflict_carries_quarantine_warning_and_data_note(vault):
    vault.config.settings["opinion_update_threshold"] = 0.30
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    vault.store("Max prefers Zoho for the custom-domain mail",
                caller="test", kind="opinion", quarantined=True)
    res = vault.store("Max prefers Fastmail for the custom-domain mail",
                      caller="test", kind="opinion")
    assert res["stored"] is False
    assert res["conflicts"][0]["quarantined"] is True
    assert "warning" in res["conflicts"][0]
    assert "note" in res                        # data, not instructions


def test_reaffirm_applies_weight_tags_and_expiry(vault, vault_path):
    a = vault.store("Max prefers rsync for the NAS backups", caller="test",
                    kind="opinion", tags=["backup"])
    r = vault.store("Max prefers rsync for the NAS backups", caller="test",
                    kind="opinion", importance=0.95, tags=["nas"],
                    expires="30d")
    assert r["reaffirmed"] and r["applied"]["importance"] == 0.95
    got = vault.get(a["id"], caller="test")
    assert got["importance"] == 0.95 and got["expires"]
    assert set(got["tags"]) >= {"backup", "nas"}
    del vault                                   # journal only, no save
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    got = v2.get(a["id"], caller="test")
    assert got["importance"] == 0.95 and got["expires"]
    assert set(got["tags"]) >= {"backup", "nas"}


def test_forgetting_the_replacement_restores_the_predecessor(vault, vault_path):
    a = vault.store("The API tier in use is the free one", caller="test")
    b = vault.store("The API tier in use is the paid one", caller="test",
                    supersedes=[a["id"]])
    out = vault.forget(b["id"], caller="test")
    assert out["restored"] == [a["id"]]
    assert not vault.get(a["id"], caller="test").get("superseded_by")
    ids = [r["id"] for r in
           vault.search("which API tier is in use", caller="test")["results"]]
    assert a["id"] in ids                       # live index has it back
    del vault                                   # replay the same journal
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    ids = [r["id"] for r in
           v2.search("which API tier is in use", caller="test")["results"]]
    assert a["id"] in ids


def test_expiry_spares_tombstones_and_releases_predecessors(vault):
    a = vault.store("The office wifi password is hunter2", caller="test")
    b = vault.store("The office wifi password is hunter3", caller="test",
                    supersedes=[a["id"]], expires="2d")
    # the tombstone itself never expires, even with a past date on it
    vault.db.conn.execute(
        "UPDATE records SET expires = '2020-01-01' WHERE id = ?", (a["id"],))
    assert vault.expire(caller="test")["removed"] == 0
    # an expiring replacement releases what it superseded
    vault.db.conn.execute(
        "UPDATE records SET expires = '2020-01-01' WHERE id = ?", (b["id"],))
    out = vault.expire(caller="test")
    assert out["ids"] == [b["id"]]
    assert not vault.get(a["id"], caller="test").get("superseded_by")


def test_status_counts_compose(vault):
    a = vault.store("The API tier in use is the free one", caller="test")
    vault.store("The API tier in use is the paid one", caller="test",
                supersedes=[a["id"]])
    st = vault.status()
    assert st["records"] == 1 and st["superseded_records"] == 1
    assert st["organic_records"] == 1


def test_supersede_requires_a_grant_on_the_replacement(vault):
    from compartment.acl import AclError
    a = vault.store("Max prefers window seats", caller="test",
                    namespace="main", kind="opinion")
    b = vault.store("Max prefers aisle seats", caller="test",
                    namespace="secret", kind="opinion", supersedes=[])
    vault.config.callers["bot"] = {"default_namespace": "main",
                                   "grants": {"main": "rw"}}
    with pytest.raises(AclError):
        vault.supersede(a["id"], b["id"], caller="bot")


def test_opinion_clusters_never_cross_namespaces(vault):
    vault.config.settings["opinion_reaffirm_threshold"] = 1.01
    vault.store("Max prefers Zoho for the custom-domain mail",
                caller="test", namespace="main", kind="opinion")
    vault.store("Max prefers Fastmail for the custom-domain mail",
                caller="test", namespace="secret", kind="opinion")
    clusters = curate.opinion_clusters(vault, threshold=0.30)
    for cluster in clusters:
        assert len({e["namespace"] for e in cluster}) == 1
    assert not clusters                        # 1 opinion per ns: no cluster


def test_apply_plan_validates_kind_importance_and_duplicate_ids(vault):
    old = vault.store("blob " * 100, caller="test", _gate=False)
    for bad in ([{"id": old["id"], "pieces": [{"text": "ok", "kind": "belief"}]}],
                [{"id": old["id"], "pieces": [{"text": "ok",
                                               "importance": "high"}]}],
                [{"id": old["id"], "pieces": [{"text": "ok"}]},
                 {"id": old["id"], "pieces": [{"text": "again"}]}]):
        with pytest.raises(CryptoError):
            curate.apply_plan(vault, bad, caller="test")
    assert vault.db.count() == 1               # nothing was written
    # a null importance is a fallback to the blob's own, not a crash
    out = curate.apply_plan(vault, [{"id": old["id"], "pieces": [
        {"text": "the blob's one real claim", "importance": None}]}],
        caller="test")
    assert out["blobs_superseded"] == 1


def test_apply_plan_never_anchors_a_blob_to_itself(vault):
    vault.config.settings["duplicate_threshold"] = 0.30
    old = vault.store("The Vault-Publishing folder moved to Desktop. " * 8,
                      caller="test", _gate=False)
    out = curate.apply_plan(vault, [{"id": old["id"], "pieces": [
        {"text": "Vault-Publishing moved to ~/Desktop/Publishing"},
        {"text": "Vault-Publishing holds 242 files, 634 MB"},
    ]}], caller="test")
    assert out["blobs_superseded"] == 1
    got = vault.get(old["id"], caller="test")
    assert got["superseded_by"] and got["superseded_by"] != old["id"]


def test_parse_plan_survives_unicode_line_separators():
    line = json.dumps({"id": "abc", "pieces": [
        {"text": "claim with a separator inside"}]}, ensure_ascii=False)
    plan = curate.parse_plan(line)
    assert " " in plan[0]["pieces"][0]["text"]


def test_store_many_prevalidates_the_whole_batch(vault):
    from compartment import server
    server._state["caller"] = "test"
    facts = [{"text": "a fine first claim"},
             {"text": "blob " * 100},
             {"text": "a fine third claim"}]
    with pytest.raises(server.MemoryToolError) as e:
        server._prevalidate_facts(vault, facts)
    assert "facts[1]" in str(e.value)
    assert vault.db.count() == 0
    with pytest.raises(server.MemoryToolError) as e:
        server._prevalidate_facts(vault, [
            {"text": "ok", "supersedes": ["missing"]}])
    assert "facts[0]" in str(e.value)
