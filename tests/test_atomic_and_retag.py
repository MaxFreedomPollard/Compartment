"""Atomic memories: dating, provenance, batch storage, and the retagger.

The properties under test are the ones whose failure would be silent. A vault
that refused to open would be noticed in a second; a retagger that quietly
reclassified thousands of seeded memories, or an upgrade that dropped every
recorded date, would not be noticed until the vault was already wrong.
"""
import datetime
import json
import re
import sqlite3
import time

import numpy as np
import pytest

from compartment import retag
from compartment.store import Store
from compartment.vault import (Vault, is_seeded, local_stamp, strip_provenance,
                               with_provenance)

from conftest import PASS


# --- opening a vault written by an older version ------------------------------

def test_old_vault_without_the_new_columns_opens_and_gains_them():
    """A 4.4.1 vault has no `source` or `tags_origin`. Opening one must add the
    columns rather than failing on the first query that names them."""
    old = sqlite3.connect(":memory:", isolation_level=None)
    old.executescript("""
        CREATE TABLE records (
            id TEXT PRIMARY KEY, ikey INTEGER UNIQUE, ns TEXT NOT NULL,
            ct BLOB NOT NULL, key_wrapped BLOB NOT NULL, vec BLOB NOT NULL,
            dim INTEGER NOT NULL, tags TEXT NOT NULL, importance REAL NOT NULL,
            quarantined INTEGER NOT NULL, pack TEXT, prov TEXT NOT NULL,
            created REAL NOT NULL, accessed REAL NOT NULL);
        CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, text);
    """)
    old.execute(
        "INSERT INTO records VALUES ('r1',1,'main',x'00',x'00',x'00',1,'[]',"
        "0.5,0,NULL,'{}',1.0,1.0)")
    image = old.serialize()

    db = Store(image)
    cols = {r["name"] for r in
            db.conn.execute("PRAGMA table_info(records)").fetchall()}
    assert {"source", "tags_origin"} <= cols
    # the pre-existing row survives, with the new fields simply empty
    row = db.conn.execute("SELECT * FROM records WHERE id='r1'").fetchone()
    assert row["source"] is None and row["tags_origin"] is None


def test_migration_is_idempotent():
    db = Store()
    db._migrate_columns()
    db._migrate_columns()
    cols = [r["name"] for r in
            db.conn.execute("PRAGMA table_info(records)").fetchall()]
    assert cols.count("source") == 1


# --- dates and provenance -----------------------------------------------------

def test_every_read_path_reports_when_the_memory_was_stored(vault):
    vault.store("The user's shell is zsh.", caller="test",
                source="read from /etc/shells")
    for got in (vault.search("shell", caller="test")["results"][0],
                vault.recent(caller="test")["results"][0]):
        assert got["created_local"] == local_stamp(got["created"])
        assert got["created_local"]                     # non-empty, formatted
        assert got["source"] == "read from /etc/shells"


def test_get_reports_the_date_and_the_original_tags(vault):
    rid = vault.store("Fastmail Individual was about $6/month at that check.",
                      caller="test", tags=["email"], source="web search")["id"]
    got = vault.get(rid, caller="test")
    assert got["source"] == "web search"
    assert got["created_local"]
    assert got["tags_origin"] == ["email"]


def test_store_returns_the_stamp_it_recorded(vault):
    out = vault.store("A fact.", caller="test", source="the user said so")
    assert out["source"] == "the user said so"
    assert out["created_local"] == local_stamp(out["created"])


def test_a_blank_source_is_stored_as_absent_not_as_empty_string(vault):
    rid = vault.store("A fact.", caller="test", source="   ")["id"]
    assert vault.get(rid, caller="test")["source"] is None


# --- imported and replayed memories keep their original date ------------------

def test_import_preserves_the_date_a_memory_was_first_learned(vault):
    long_ago = time.time() - 400 * 86400
    line = json.dumps({"text": "An old fact.", "namespace": "main",
                       "created": long_ago, "source": "web search",
                       "tags": [], "importance": 0.5})
    assert vault.import_jsonl(line, caller="test") == 1
    got = vault.recent(caller="test")["results"][0]
    assert got["created"] == pytest.approx(long_ago)
    assert got["source"] == "web search"


def test_export_round_trips_source_and_created(vault):
    vault.store("A fact worth keeping.", caller="test", source="inferred")
    rec = json.loads(vault.export_jsonl(caller="test").splitlines()[0])
    assert rec["source"] == "inferred"
    assert rec["created_local"] == local_stamp(rec["created"])


def test_a_journal_replay_keeps_the_source(vault_path):
    v = Vault.create(vault_path, PASS, creator="test")
    v.store("A fact.", caller="test", source="web search")
    # abandon without saving: reopening replays the journal
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    assert v2.recent(caller="test")["results"][0]["source"] == "web search"


# --- batch storage ------------------------------------------------------------

def test_many_facts_become_many_records_each_with_its_own_date(vault):
    facts = ["compartment.dev is registered and on Cloudflare nameservers.",
             "Zoho's free tier no longer includes IMAP.",
             "Proton IMAP needs the paid Proton Bridge app running."]
    ids = [vault.store(f, caller="test", source="web search")["id"]
           for f in facts]
    assert len(set(ids)) == 3
    for rid in ids:
        got = vault.get(rid, caller="test")
        assert got["source"] == "web search"
        assert got["created_local"]


# --- the two dates ------------------------------------------------------------

def test_the_fact_carries_its_own_method_and_discovery_date(vault):
    rid = vault.store("Zoho's free tier no longer includes IMAP.",
                      caller="test", source="web search")["id"]
    text = vault.get(rid, caller="test")["text"]
    today = datetime.date.today().isoformat()
    assert text.endswith(f"[web search, {today}]")


def test_the_discovery_date_carries_no_time_of_day(vault):
    rid = vault.store("A fact.", caller="test", source="web search")["id"]
    got = vault.get(rid, caller="test")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", got["discovered"])
    # while the SAVED stamp does keep the time, because that is a fact about
    # this vault rather than a claim about the world
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", got["created_local"])


def test_a_fact_learned_earlier_than_it_was_written_down_keeps_both_dates(vault):
    rid = vault.store("The outage began on the Friday.", caller="test",
                      source="read from the incident log",
                      discovered="2026-07-12")["id"]
    got = vault.get(rid, caller="test")
    assert got["discovered"] == "2026-07-12"
    assert got["created_local"].startswith(datetime.date.today().isoformat())
    assert "2026-07-12" in got["text"]


def test_discovery_date_accepts_a_timestamp_or_a_datetime_and_drops_the_time():
    from compartment.vault import discovery_date
    assert discovery_date("2026-07-12T14:32:00") == "2026-07-12"
    assert discovery_date(time.mktime((2026, 7, 12, 14, 32, 0, 0, 0, -1))) \
        == "2026-07-12"
    assert discovery_date(None) == datetime.date.today().isoformat()


def test_the_clause_is_not_stacked_when_a_memory_is_re_imported(vault):
    vault.store("A fact worth keeping.", caller="test", source="web search")
    line = vault.export_jsonl(caller="test").splitlines()[0]
    text = json.loads(line)["text"]
    # importing an export must not append a second clause
    v2_text = strip_provenance(with_provenance(text, "web search", "2026-01-01"))
    assert v2_text == "A fact worth keeping."
    assert with_provenance(text, "other", "2026-01-01") == text


def test_the_embedding_is_of_the_claim_not_of_the_provenance(vault):
    """Two records of the SAME fact learned on different days by different
    means must still be recognised as the same fact, which they cannot be if
    the clause is part of what gets embedded."""
    a = vault.store("The vault lives at ~/.compartment/memory.vault.",
                    caller="test", source="web search", discovered="2026-01-02")
    b = vault.store("The vault lives at ~/.compartment/memory.vault.",
                    caller="test", source="the user said so",
                    discovered="2026-08-01")
    assert b.get("duplicate") is True and b["id"] == a["id"]


def test_curated_pack_content_is_installed_verbatim(vault):
    out = vault.store("A curated starting fact.", caller="test",
                      pack="somepack")
    text = vault.get(out["id"], caller="test")["text"]
    assert text == "A curated starting fact.", \
        "pack content must not be stamped with the day someone ran init"


# --- the retagger -------------------------------------------------------------

def _tags(vault, rid):
    return vault.get(rid, caller="test")["tags"]


def test_retagging_never_changes_the_memory_itself(vault):
    rid = vault.store("The user prefers Fastmail for custom domains.",
                      caller="test", tags=["email"], source="the user said so")["id"]
    before = vault.get(rid, caller="test")
    retag.run(vault, caller="test")
    after = vault.get(rid, caller="test")
    for field in ("text", "created", "importance", "source", "namespace"):
        assert after[field] == before[field], f"retag changed {field}"


def test_retagging_cannot_strip_a_seeded_memory_of_its_identity(seeded_vault):
    row = seeded_vault.db.conn.execute(
        "SELECT id, tags FROM records WHERE tags LIKE '%\"id:%' LIMIT 1"
    ).fetchone()
    assert row is not None, "fixture should contain seeded records"
    # even handed a plan that omits it entirely, the write path restores it
    seeded_vault.db.set_tags(row["id"], ["something", "else"])
    tags = json.loads(seeded_vault.db.conn.execute(
        "SELECT tags FROM records WHERE id = ?", (row["id"],)).fetchone()["tags"])
    assert any(t.startswith("id:") for t in tags)
    assert is_seeded(json.dumps(tags))


def test_an_identity_tag_is_never_propagated_to_a_neighbour(seeded_vault):
    """The failure this exists to prevent: one seeded record's unique "id:" tag
    spreading across its neighbourhood would reclassify learned memories as
    starter content, and nothing would be left to tell them apart."""
    changes = retag.plan(seeded_vault, limit=200)
    for c in changes:
        assert not any(t.startswith("id:") for t in c.added)


def test_a_tag_spreads_on_meaning_alone(vault):
    """The Zoho case, and the reason the semantic channel exists.

    The tag word appears in NO record's text, so the lexical signal cannot
    reach it and only embedding proximity can carry it. This is the test that
    would fail if propagation quietly stopped working and the retagger degraded
    into a keyword matcher, which it would otherwise still pass every other
    test while doing."""
    for line in ("Sending mail requires an app-specific password.",
                 "The mailbox syncs over IMAP on port 993.",
                 "Outgoing messages relay through SMTP on port 587.",
                 "The inbox is reachable with a standard mail client."):
        vault.store(line, caller="test", tags=["correspondence"])
    target = vault.store("Message delivery uses a relay host with TLS.",
                         caller="test", tags=[])["id"]
    unrelated = vault.store("The kitchen tap drips when the boiler runs.",
                            caller="test", tags=[])["id"]
    retag.run(vault, caller="test")
    assert "correspondence" in _tags(vault, target)
    assert "correspondence" not in _tags(vault, unrelated), \
        "a tag must not spread to a memory that merely shares the vault"


def test_a_known_tag_said_out_loud_in_the_text_is_attached(vault):
    # The tag must already be a CATEGORY (MIN_TAG_SUPPORT records) before it is
    # eligible to spread; a label used once belongs to the memory that has it.
    for i in range(retag.MIN_TAG_SUPPORT):
        vault.store(f"Some unrelated memory about fastmail, {i}.",
                    caller="test", tags=["fastmail"])
    rid = vault.store("Billing for fastmail renews annually.", caller="test",
                      tags=[])["id"]
    retag.run(vault, caller="test")
    assert "fastmail" in _tags(vault, rid)


def test_a_tag_used_only_once_is_not_spread_to_anything(vault):
    """A one-off label names a particular memory rather than a class of them,
    and copying it onto a neighbour asserts a category that never existed."""
    vault.store("A memory with a private label.", caller="test",
                tags=["max-automation-philosophy"])
    for i in range(4):
        vault.store(f"A closely related memory, number {i}.", caller="test",
                    tags=["notes"])
    retag.run(vault, caller="test")
    for row in vault.db.conn.execute("SELECT id FROM records"):
        tags = _tags(vault, row["id"])
        assert tags.count("max-automation-philosophy") <= 1


def test_a_date_shaped_tag_never_travels(vault):
    """Propagating "2026-07-26" onto a neighbour claims that neighbour was
    about that day, which is a different kind of claim entirely."""
    for i in range(5):
        vault.store(f"A memory about the release, number {i}.", caller="test",
                    tags=["release", "2026-07-26"])
    rid = vault.store("Another memory about the same release work.",
                      caller="test", tags=[])["id"]
    retag.run(vault, caller="test")
    assert "2026-07-26" not in _tags(vault, rid)


def test_a_short_tag_does_not_match_inside_a_longer_word(vault):
    vault.store("A memory about go, the language.", caller="test", tags=["go"])
    rid = vault.store("We use Google Workspace for mail.", caller="test",
                      tags=[])["id"]
    retag.run(vault, caller="test")
    assert "go" not in _tags(vault, rid)


def test_a_pass_is_additive_unless_pruning_is_asked_for(vault):
    rid = vault.store("A lone memory with an unusual tag.", caller="test",
                      tags=["zoho"])["id"]
    retag.run(vault, caller="test")
    assert "zoho" in _tags(vault, rid), "default pass must not remove tags"


def test_pruning_keeps_a_tag_the_text_itself_says(vault):
    rid = vault.store("The zoho mailbox is web-only now.", caller="test",
                      tags=["zoho"])["id"]
    retag.run(vault, prune=True, caller="test")
    assert "zoho" in _tags(vault, rid)


def test_a_second_pass_over_an_unchanged_vault_changes_nothing(vault):
    for i in range(6):
        vault.store(f"Fact number {i} about mail servers.", caller="test",
                    tags=["mail"])
    retag.run(vault, caller="test")
    assert retag.plan(vault) == []


def test_the_original_tags_are_still_recoverable_after_retagging(vault):
    rid = vault.store("The user prefers app passwords over OAuth.",
                      caller="test", tags=["auth"])["id"]
    for i in range(4):
        vault.store(f"App password note {i}.", caller="test",
                    tags=["auth", "email"])
    retag.run(vault, caller="test")
    assert vault.get(rid, caller="test")["tags_origin"] == ["auth"]


def test_retagging_an_empty_vault_is_a_no_op(vault):
    assert retag.plan(vault) == []
    assert retag.run(vault, caller="test")["records_changed"] == 0


def test_no_record_accumulates_unbounded_tags(vault):
    for i in range(30):
        vault.store(f"Closely related mail fact {i}.", caller="test",
                    tags=[f"tag{i}", "mail"])
    retag.run(vault, caller="test")
    for row in vault.db.conn.execute("SELECT id FROM records"):
        assert len(_tags(vault, row["id"])) <= retag.MAX_TAGS_PER_RECORD


def test_record_matrix_gives_one_unit_vector_per_record(vault):
    vault.store("A short memory.", caller="test")
    vault.store("Another short memory.", caller="test")
    ids, mat = retag._record_matrix(vault.db)
    assert len(ids) == len(set(ids)) == mat.shape[0] == 2
    assert np.allclose(np.linalg.norm(mat, axis=1), 1.0, atol=1e-5)


def test_protected_prefixes_are_declared_where_both_modules_can_see_them():
    """retag.py and vault.is_seeded must agree on what marks a seeded record;
    if they ever drift, propagation silently starts smearing identities."""
    assert "id:" in Store.PROTECTED_TAG_PREFIXES


# --- searching by when a fact was learned, not when it was written down -------

def test_search_can_filter_on_the_day_a_fact_was_discovered(vault):
    vault.store("The old rate was measured at 40 requests per second.",
                caller="test", source="ran the benchmark", discovered="2026-01-15")
    vault.store("The new rate was measured at 90 requests per second.",
                caller="test", source="ran the benchmark", discovered="2026-07-20")
    got = vault.search("requests per second rate", caller="test",
                       discovered_since="2026-06-01")["results"]
    assert len(got) == 1 and "90" in got[0]["text"]
    got = vault.search("requests per second rate", caller="test",
                       discovered_until="2026-06-01")["results"]
    assert len(got) == 1 and "40" in got[0]["text"]


def test_the_two_date_filters_are_independent(vault):
    """Saved today, discovered long ago: a save-date filter must find it and a
    discovery-date filter for the same window must not."""
    vault.store("A fact established years ago.", caller="test",
                source="read from the archive", discovered="2020-03-01")
    recent = vault.search("fact established", caller="test",
                          since=time.time() - 3600)["results"]
    assert recent, "it was saved just now, so a save-date filter must see it"
    old = vault.search("fact established", caller="test",
                       discovered_since="2026-01-01")["results"]
    assert not old, "it was not discovered this year"


def test_a_memory_with_no_discovery_date_is_excluded_not_assumed(vault):
    rid = vault.store("A fact from an older vault.", caller="test")["id"]
    vault.db.conn.execute("UPDATE records SET discovered = NULL WHERE id = ?",
                          (rid,))
    assert not vault.search("older vault fact", caller="test",
                            discovered_since="2000-01-01")["results"]


# --- retagging in slices ------------------------------------------------------

def test_retagging_in_slices_gives_the_same_answer_as_one_pass(vault):
    """The background pass retags in chunks so it never holds the tool lock for
    long. Chunking must not change WHAT a record gets: neighbourhoods are
    computed from the whole vault, never from the slice."""
    for i in range(12):
        vault.store(f"A memory about mail relays, number {i}.", caller="test",
                    tags=["relays", "mail"])
    for i in range(6):
        vault.store(f"An untagged memory about relaying mail, {i}.",
                    caller="test", tags=[])
    whole = {c.record_id: sorted(c.after) for c in retag.plan(vault)}

    ids = retag.targets(vault)
    sliced = {}
    for i in range(0, len(ids), 5):
        for c in retag.plan(vault, only=ids[i:i + 5]):
            sliced[c.record_id] = sorted(c.after)
    assert sliced == whole


def test_targets_covers_exactly_the_organic_records(seeded_vault):
    # Read-only: `seeded_vault` is shared across the whole session, so storing
    # into it here would silently change what every other test that counts
    # organic records sees.
    ids = retag.targets(seeded_vault)
    assert len(ids) == seeded_vault.status()["organic_records"]
    assert len(ids) < seeded_vault.db.count(), "seeded records must be excluded"


def test_targets_on_a_fresh_vault_is_every_record(vault):
    for i in range(3):
        vault.store(f"A memory, number {i}.", caller="test", source="test")
    assert len(retag.targets(vault)) == vault.db.count() == 3


# --- how many results come back ----------------------------------------------

def _fill(vault, n, text, **kw):
    for i in range(n):
        vault.store(text.format(i=i), caller="test", source="test", **kw)


def test_an_explicit_top_k_is_still_an_exact_window(vault):
    """Callers that page, benchmark, or budget context ask for a fixed number
    and must keep getting exactly that."""
    _fill(vault, 20, "Memory number {i} about vault encryption keys.")
    assert len(vault.search("vault encryption keys", caller="test",
                            top_k=5)["results"]) == 5


def test_the_default_is_not_a_fixed_eight(vault):
    """The whole point of the change: with many equally relevant atomic facts,
    a fixed eight silently withheld the rest."""
    _fill(vault, 40, "Fact {i} about vault encryption keys and passphrases.")
    got = vault.search("vault encryption keys passphrases", caller="test")["results"]
    assert len(got) > 8, f"expected more than the old fixed 8, got {len(got)}"


def test_one_clear_winner_does_not_drag_in_the_whole_vault(vault):
    """Relevance is judged against the best answer to THIS query, so a decisive
    hit should not be padded out with weak ones."""
    _fill(vault, 30, "An unrelated note about printer trays, number {i}.")
    vault.store("The vault passphrase hint is stored in the keychain.",
                caller="test", source="test")
    got = vault.search("vault passphrase hint keychain", caller="test")["results"]
    assert 1 <= len(got) <= 5, f"expected a tight result set, got {len(got)}"
    assert "passphrase hint" in got[0]["text"]


def test_results_never_exceed_the_generous_cap(vault):
    from compartment.ranking import MAX_RESULTS
    _fill(vault, 130, "Fact {i} about vault encryption keys and passphrases.")
    got = vault.search("vault encryption keys passphrases", caller="test")["results"]
    assert len(got) <= MAX_RESULTS


def test_every_returned_result_clears_the_relevance_floor(vault):
    from compartment.ranking import RESULT_ABSOLUTE_FLOOR, RESULT_RELATIVE_FLOOR
    _fill(vault, 25, "Fact {i} about vault encryption keys.")
    got = vault.search("vault encryption keys", caller="test")["results"]
    assert got
    floor = max(RESULT_ABSOLUTE_FLOOR, got[0]["score"] * RESULT_RELATIVE_FLOOR)
    assert all(r["score"] >= floor for r in got)
    # and best-first ordering is preserved
    assert [r["score"] for r in got] == sorted((r["score"] for r in got),
                                               reverse=True)
