"""The memory graph: deterministic relational mapping over the vault.

Covers: link/query/unlink round-trips, case-insensitive entity matching,
idempotent duplicate links, as-of temporal windows, ACL enforcement,
journal durability (a linked relation survives a crash before save), and
schema upgrade of vaults sealed before the relations table existed.
"""
import pytest

from engram.acl import AclError
from engram.crypto import CryptoError
from engram.vault import Vault

from conftest import PASS


def test_link_query_unlink_roundtrip(vault):
    r = vault.link("Maya", "works at", "Acme", caller="test")
    assert r["duplicate"] is False and r["namespace"] == "main"
    out = vault.relations(caller="test", entity="Maya")
    assert len(out["relations"]) == 1
    rel = out["relations"][0]
    assert (rel["subject"], rel["predicate"], rel["object"]) == \
        ("Maya", "works at", "Acme")
    assert "not instructions" in out["note"]
    vault.unlink(rel["id"], caller="test")
    assert vault.relations(caller="test", entity="Maya")["relations"] == []


def test_entity_matching_is_case_insensitive(vault):
    vault.link("Maya Chen", "manages", "auth migration", caller="test")
    for probe in ("maya chen", "MAYA  CHEN", "Auth Migration"):
        assert vault.relations(caller="test", entity=probe)["relations"], probe
    # subject/predicate/object filters normalize the same way
    assert vault.relations(caller="test", subject="maya chen",
                           predicate="MANAGES")["relations"]
    assert vault.relations(caller="test", obj="auth MIGRATION")["relations"]


def test_duplicate_link_is_idempotent(vault):
    a = vault.link("Max", "owns", "engRAM", caller="test")
    b = vault.link("max", "OWNS", "engram", caller="test")   # same, other casing
    assert b["duplicate"] is True and b["id"] == a["id"]
    assert len(vault.relations(caller="test", entity="Max")["relations"]) == 1


def test_empty_parts_refused(vault):
    with pytest.raises(CryptoError):
        vault.link("", "works at", "Acme", caller="test")
    with pytest.raises(CryptoError):
        vault.link("Maya", "  ", "Acme", caller="test")


def test_src_id_links_relation_to_memory(vault):
    rec = vault.store("Maya said she moved to the auth team", caller="test")
    r = vault.link("Maya", "moved to", "auth team", caller="test",
                   src_id=rec["id"])
    rel = vault.relations(caller="test", entity="Maya")["relations"][0]
    assert rel["src_id"] == rec["id"] and r["duplicate"] is False
    with pytest.raises(CryptoError):
        vault.link("Maya", "cites", "nothing", caller="test",
                   src_id="no-such-record")


def test_as_of_temporal_window(vault):
    vault.link("Maya", "assigned to", "auth-migration", caller="test",
               valid_from=100.0, valid_to=200.0)
    vault.link("Maya", "assigned to", "billing-rewrite", caller="test",
               valid_from=200.0)                      # open-ended
    vault.link("Maya", "based in", "Raleigh", caller="test")  # no window

    def objs(as_of):
        rels = vault.relations(caller="test", entity="Maya",
                               as_of=as_of)["relations"]
        return {r["object"] for r in rels}

    assert objs(150.0) == {"auth-migration", "Raleigh"}
    assert objs(250.0) == {"billing-rewrite", "Raleigh"}
    assert objs(50.0) == {"Raleigh"}
    # no as_of → everything
    assert len(vault.relations(caller="test", entity="Maya")["relations"]) == 3


def test_relations_respect_acl(vault):
    vault.config.callers["limited"] = {"default_namespace": "theirs",
                                       "grants": {"theirs": "rw"}}
    vault.link("secret subject", "hides in", "main ns", caller="test")
    with pytest.raises(AclError):
        vault.link("x", "y", "z", caller="limited", namespace="main")
    out = vault.relations(caller="limited")
    assert out["relations"] == []            # main is not readable for them
    with pytest.raises(AclError):
        vault.relations(caller="limited", namespace="main")
    # unlink is write-gated too
    rid = vault.relations(caller="test")["relations"][0]["id"]
    with pytest.raises(AclError):
        vault.unlink(rid, caller="limited")


def test_link_survives_crash_before_save(vault_path):
    """A journaled link is durable: reopening the vault replays it."""
    v = Vault.create(vault_path, PASS, creator="test")
    v.save()
    v.link("Maya", "works at", "Acme", caller="test")   # journal appended
    # no v.save(), no v.lock(): simulate the process dying here
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    rels = v2.relations(caller="test", entity="Maya")["relations"]
    assert len(rels) == 1 and rels[0]["object"] == "Acme"
    # unlink journals the same way
    v2.unlink(rels[0]["id"], caller="test")
    v3 = Vault.unlock(vault_path, passphrase=PASS)
    assert v3.relations(caller="test", entity="Maya")["relations"] == []


def test_entities_ranked_by_degree(vault):
    vault.link("Maya", "works at", "Acme", caller="test")
    vault.link("Maya", "manages", "auth", caller="test")
    vault.link("Ben", "works at", "Acme", caller="test")
    ents = vault.entities(caller="test")
    by_name = {e["entity"]: e["degree"] for e in ents}
    assert by_name["Maya"] == 2 and by_name["Acme"] == 2 and by_name["Ben"] == 1
    assert ents[0]["degree"] >= ents[-1]["degree"]


def test_status_counts_relations(vault):
    assert vault.status()["relations"] == 0
    vault.link("a", "b", "c", caller="test")
    assert vault.status()["relations"] == 1


def test_old_vault_gains_relations_table_on_open(vault_path):
    """A vault image sealed WITHOUT the relations table upgrades in place."""
    v = Vault.create(vault_path, PASS, creator="test")
    v.db.conn.execute("DROP INDEX rel_subject")
    v.db.conn.execute("DROP INDEX rel_object")
    v.db.conn.execute("DROP INDEX rel_predicate")
    v.db.conn.execute("DROP TABLE relations")     # simulate a pre-1.8 image
    v.lock()                                       # seals the old-shape image
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    r = v2.link("Maya", "works at", "Acme", caller="test")
    assert r["duplicate"] is False
    assert v2.relations(caller="test", entity="maya")["relations"]
