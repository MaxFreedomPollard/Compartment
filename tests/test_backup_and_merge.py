"""One-click encrypted .vault backup, lossless in-RAM .vault merge, and
organic/relation JSONL portability across macOS, Windows, Linux, and the CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys

import numpy as np
import pytest

from compartment import cli, menubar, packs, systray, vaultfile
from compartment.crypto import CryptoError, TamperError
from compartment.vault import Vault

from conftest import PASS


def _run_cli(monkeypatch, capsys, argv: list[str], stdin_text: str | None = None):
    if stdin_text is not None:
        import io
        raw = stdin_text.encode("utf-8")
        stream = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        monkeypatch.setattr(sys, "stdin", stream)
    else:
        monkeypatch.setattr("compartment.cli._can_prompt", lambda: False)
    cli.main(argv)
    return capsys.readouterr()


# ------------------------------------------------ encrypted .vault backups

def test_backup_works_while_locked_and_preserves_v1_encryption(tmp_path):
    src = str(tmp_path / "live.vault")
    v = Vault.create(src, PASS, creator="alice")
    rid = v.store("Primary staging database runs on port 5433.",
                  caller="alice", source="ops doc")["id"]
    v.link("staging-db", "listens_on", "5433", caller="alice", src_id=rid)
    # Leave the store and relation in the encrypted append-only journal (no
    # compaction via save()) to prove backup captures both snapshot + journal.
    from compartment import session
    session.clear(src)
    del v

    dest = str(tmp_path / "backups" / "portable.vault")
    info = Vault.backup(src, dest)
    assert info["vault_id"]
    assert info["format_version"] == vaultfile.FORMAT_VERSION == 1
    assert info["journal_entries"] >= 2
    assert info["stripped_torn_bytes"] == 0

    raw = open(dest, "rb").read()
    assert raw[:4] == vaultfile.MAGIC == b"NUCV"
    assert hashlib.sha256(raw).hexdigest() == info["sha256"]
    assert b"Primary staging database" not in raw
    assert b"staging-db" not in raw
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600

    # Wrong passphrase fails AEAD unwrap; right passphrase recovers everything.
    with pytest.raises(CryptoError):
        Vault.unlock(dest, passphrase="wrong passphrase")
    restored = Vault.unlock(dest, passphrase=PASS)
    got = restored.get(rid, caller="alice")
    assert "5433" in got["text"]
    rels = restored.relations(entity="staging-db", caller="alice")["relations"]
    assert len(rels) == 1 and rels[0]["object"] == "5433"


def test_backup_strips_crashed_torn_journal_tail_and_verifies_signatures(tmp_path):
    src = str(tmp_path / "signed.vault")
    ident = packs.new_identity("alice")
    sk = packs.load_signing_key(ident)
    v = Vault.create(src, PASS, creator="alice")
    v.store(" Signing key check across releases.", caller="alice",
            source="test")
    v.lock(signing_key=sk)

    # Simulate a power-cut leaving 5 torn bytes at the tail of the file.
    with open(src, "ab") as f:
        f.write(b"\xfe\xed\xfa\xce\x00")

    dest = str(tmp_path / "clean-signed.vault")
    info = Vault.backup(src, dest)
    assert info["signed"] is True
    assert info["stripped_torn_bytes"] == 5
    loaded = vaultfile.read_vault_file(dest)
    assert loaded.journal_end == os.path.getsize(dest)
    assert vaultfile.verify_manifest(loaded)["creator"] == "alice"

    # Corrupting a signed vault's ciphertext is caught before writing a backup.
    bad_src = str(tmp_path / "tampered.vault")
    data = bytearray(open(dest, "rb").read())
    data[-10] ^= 0xFF
    open(bad_src, "wb").write(data)
    bad_dest = str(tmp_path / "never-written.vault")
    with pytest.raises(TamperError):
        Vault.backup(bad_src, bad_dest)
    assert not os.path.exists(bad_dest)


def test_backup_refuses_same_path_and_non_vault_files(tmp_path):
    src = str(tmp_path / "v.vault")
    v = Vault.create(src, PASS, creator="alice")
    with pytest.raises(CryptoError, match="same file"):
        v.backup_to(src)

    not_a_vault = str(tmp_path / "plain.txt")
    open(not_a_vault, "w", encoding="utf-8").write("not a vault")
    with pytest.raises(CryptoError):
        Vault.backup(not_a_vault, str(tmp_path / "out.vault"))


# ------------------------------------------------ lossless encrypted merge

def test_merge_same_master_key_needs_no_passphrase_or_onnx_and_reconciles(
        tmp_path, monkeypatch):
    # Base vault created on Machine A and copied to Machine B.
    path_a = str(tmp_path / "machine_a.vault")
    path_b = str(tmp_path / "machine_b.vault")
    va = Vault.create(path_a, PASS, creator="user")
    # Seed a starter-pack style record on both machines to verify O(1) skip
    # and relation src_id remapping.
    seed_a = va.store(" macOS stores user LaunchAgents in ~/Library/LaunchAgents.",
                      caller="user", pack="starter",
                      tags=["id:starter-launchagents", "macos"])["id"]
    shared = va.store("Build artifacts live under dist/wheels.",
                      caller="user", tags=["build"], importance=0.5,
                      source="chat")["id"]
    old_op = va.store("Prefer SQLite WAL mode for local queues.",
                      caller="user", kind="opinion", source="chat")["id"]
    va.save()
    Vault.backup(path_a, path_b)

    # Machine A continues working offline.
    only_a = va.store("Laptop SSH key is ed25519_work_2026.",
                      caller="user", source="chat")["id"]
    va.save()

    # Machine B works independently offline: updates shared tags/importance,
    # supersedes old_op, adds a new memory + relation, and links to starter.
    vb = Vault.unlock(path_b, passphrase=PASS)
    vb.db.set_tags(shared, ["build", "release"])
    vb.db.reaffirm(shared, 4_000_000_000.0)
    vb.db.conn.execute("UPDATE records SET importance = 0.9 WHERE id = ?",
                       (shared,))
    new_op = vb.store("Prefer append-only encrypted journals over WAL files.",
                      caller="user", kind="opinion", supersedes=[old_op],
                      source="design review")["id"]
    only_b = vb.store("Desktop GPU workstation hostname is titan-linux.",
                      caller="user", source="chat")["id"]
    vb.link("titan-linux", "runs_os", "Debian 12", caller="user",
            src_id=only_b)
    vb.link("LaunchAgents", "documented_in", "starter", caller="user",
            src_id=seed_a)
    vb.save()
    del vb

    backup_bytes_before = open(path_b, "rb").read()

    # Forbid ONNX model loading during same-model merge: vectors copy directly!
    def _no_embed(*_a, **_k):
        raise AssertionError("same-model merge must reuse vectors without ONNX")

    monkeypatch.setattr(va, "_embedder",
                        type("_NoEmbed", (), {"embed_record": _no_embed})())

    res = va.merge(path_b)
    assert res["reembedded"] is False
    assert res["imported"] == 2          # new_op + only_b
    assert res["seeded_skipped"] == 1    # starter record skipped, never duplicated
    assert res["updated"] >= 1           # shared record affirmed/tags/importance
    assert res["superseded"] == 1        # old_op superseded by new_op
    assert res["relations_added"] == 2

    # Backup file on disk was never modified.
    assert open(path_b, "rb").read() == backup_bytes_before

    # Reopen Machine A from disk to prove durability.
    reopened = Vault.unlock(path_a, passphrase=PASS)
    assert reopened.get(only_a, caller="user")
    shared_rec = reopened.get(shared, caller="user")
    assert "release" in shared_rec["tags"] and "build" in shared_rec["tags"]
    assert shared_rec["importance"] == pytest.approx(0.9)
    assert shared_rec["affirmed"] == pytest.approx(4_000_000_000.0)

    # old_op is now superseded on Machine A too.
    old_row = reopened.db.get_row(old_op)
    assert old_row["superseded_by"] is not None

    # Starter record was not duplicated, and relation pointing to starter remapped!
    starter_rows = [
        r for r in reopened.db.conn.execute(
            "SELECT id FROM records WHERE pack = 'starter'").fetchall()
    ]
    assert len(starter_rows) == 1 and starter_rows[0][0] == seed_a
    la_rels = reopened.relations(subject="LaunchAgents",
                                 caller="user")["relations"]
    assert len(la_rels) == 1 and la_rels[0]["src_id"] == seed_a

    # Relation on only_b remapped to its record id in Machine A.
    titan_rels = reopened.relations(subject="titan-linux",
                                    caller="user")["relations"]
    assert len(titan_rels) == 1
    linked_mem = reopened.get(titan_rels[0]["src_id"], caller="user")
    assert "titan-linux" in linked_mem["text"]

    # Re-running merge on the exact same backup is an idempotent no-op.
    second = reopened.merge(path_b)
    assert second["imported"] == 0
    assert second["relations_added"] == 0


def test_merge_independent_passphrase_and_2fa_keyfile_and_cross_model(tmp_path):
    path_a = str(tmp_path / "a.vault")
    path_b = str(tmp_path / "b.vault")
    va = Vault.create(path_a, PASS, creator="user")
    va.store("Alpha memory on laptop.", caller="user", source="chat")
    va.save()

    other_pw = "DifferentHorseBattery"
    vb = Vault.create(path_b, other_pw, creator="user")
    vb.store("Beta memory from a separate desktop vault.",
             caller="user", source="chat")
    # Simulate a model upgrade by changing source model_sha256 so merge
    # exercises the offline re-embedding path.
    vb.db.set_meta("model_sha256", "0" * 64)
    vb.header.model = {**vb.header.model, "sha256": "0" * 64}
    kf_bytes = os.urandom(32)
    vb.twofa_enable(other_pw, kf_bytes)
    del vb

    # Without passphrase, merge asks for the backup's credential cleanly.
    with pytest.raises(CryptoError, match="different master key"):
        va.merge(path_b)

    res = va.merge(path_b, passphrase=other_pw, keyfile=kf_bytes)
    assert res["imported"] == 1
    assert res["reembedded"] is True
    hits = va.search("Beta memory separate desktop", caller="user")["results"]
    assert hits and "Beta memory" in hits[0]["text"]


# ------------------------------------------------ CLI + GUI + JSONL round-trip

def test_cli_backup_merge_and_organic_relations_jsonl(tmp_path, monkeypatch,
                                                      capsys):
    v1_path = str(tmp_path / "one.vault")
    v2_path = str(tmp_path / "two.vault")
    v1 = Vault.create(v1_path, PASS, creator="user")
    v1.store("Starter reference fact.", caller="user", pack="starter",
             tags=["id:s1"])
    rid = v1.store("Production Redis cache runs on port 6380.",
                   caller="user", source="infra",
                   prov={"host": "box1", "agent": "claude", "session": "s1",
                         "project": "acme/payments"})["id"]
    v1.link("payments-redis", "uses_port", "6380", caller="user", src_id=rid)
    v1.save()
    from compartment import session
    session.clear(v1_path)
    del v1

    # CLI backup while locked (no passphrase prompt or env var).
    bak_path = str(tmp_path / "cli-backup.vault")
    out = _run_cli(monkeypatch, capsys,
                   ["--vault", v1_path, "backup", bak_path, "--json"]).out
    bak_doc = json.loads(out)
    assert bak_doc["dest"] == os.path.abspath(bak_path)
    assert os.path.isfile(bak_path)

    # Create a second vault with a different passphrase (with starter fact), unlock it, and merge via CLI stdin.
    v2 = Vault.create(v2_path, "SecondPassphrase!", creator="user")
    v2.store("Starter reference fact.", caller="user", pack="starter",
             tags=["id:s1"])
    v2.save()
    del v2
    _run_cli(monkeypatch, capsys,
             ["--vault", v2_path, "unlock", "--passphrase-stdin"],
             stdin_text="SecondPassphrase!\n")
    out = _run_cli(
        monkeypatch, capsys,
        ["--vault", v2_path, "merge", bak_path,
         "--source-passphrase-stdin", "--json"],
        stdin_text=PASS + "\n").out
    merge_doc = json.loads(out)
    assert merge_doc["imported"] == 1
    assert merge_doc["seeded_skipped"] == 1
    assert merge_doc["relations_added"] == 1

    # Export organic-only + relations as JSONL and import into a fresh 3rd vault.
    jsonl_path = str(tmp_path / "organic.jsonl")
    _run_cli(monkeypatch, capsys,
             ["--vault", v2_path, "export", jsonl_path, "--plaintext",
              "--organic-only", "--include-relations"])
    lines = [json.loads(ln)
             for ln in open(jsonl_path, encoding="utf-8").read().splitlines()
             if ln.strip()]
    assert len(lines) == 2
    assert not any(ln.get("pack") == "starter" for ln in lines)
    assert any(ln.get("type") == "relation" for ln in lines)

    v3_path = str(tmp_path / "three.vault")
    v3 = Vault.create(v3_path, "ThirdPassphrase!", creator="user")
    v3.save()
    del v3
    _run_cli(monkeypatch, capsys,
             ["--vault", v3_path, "unlock", "--passphrase-stdin"],
             stdin_text="ThirdPassphrase!\n")
    _run_cli(monkeypatch, capsys,
             ["--vault", v3_path, "import", jsonl_path])
    v3 = Vault.unlock(v3_path, passphrase="ThirdPassphrase!")
    rels = v3.relations(subject="payments-redis", caller="user")["relations"]
    assert len(rels) == 1 and rels[0]["object"] == "6380"
    linked = v3.get(rels[0]["src_id"], caller="user")
    assert "6380" in linked["text"]
    assert linked["provenance"].get("project") == "acme/payments"


def test_gui_data_layer_and_panel_rows_across_lock_states(monkeypatch,
                                                          tmp_path):
    assert menubar.default_backup_filename().endswith(".vault")

    # Passphrase in merge_vault goes over stdin, never argv; "different master
    # key" surfaces needs_passphrase=True so the panel opens its secure field.
    seen = {}

    def fake_need_pw(argv, **kw):
        seen["argv1"] = list(argv)
        return subprocess.CompletedProcess(
            argv, 1, "", "error: backup uses a different master key")

    monkeypatch.setattr(menubar.subprocess, "run", fake_need_pw)
    ok, msg, needs_pw = menubar.merge_vault("/v.vault", "/b.vault")
    assert not ok and needs_pw is True and "b.vault" in msg

    def fake_with_pw(argv, **kw):
        seen["argv2"] = list(argv)
        seen["input2"] = kw.get("input")
        return subprocess.CompletedProcess(
            argv, 0,
            json.dumps({"imported": 3, "duplicates": 1, "updated": 0,
                        "relations_added": 2}),
            "")

    monkeypatch.setattr(menubar.subprocess, "run", fake_with_pw)
    ok, msg, needs_pw = menubar.merge_vault("/v.vault", "/b.vault",
                                            passphrase="SecretPw")
    assert ok is True and needs_pw is False
    assert "Merged 3 memories" in msg and "2 relations" in msg
    assert "SecretPw" not in " ".join(seen["argv2"])
    assert "--source-passphrase-stdin" in seen["argv2"]
    assert seen["input2"] == "SecretPw\n"

    # Windows + Linux shared panel_rows contract across missing/locked/unknown/open.
    base = {"vault": "/v", "records": 10, "organic": 2, "recent": [],
            "error": None, "integrations": {},
            "settings": dict(menubar.DEFAULT_SETTINGS)}
    no_vault_kinds = [k for k, _ in systray.panel_rows({**base, "exists": False,
                                                        "locked": True})]
    assert "backup" not in no_vault_kinds and "merge" not in no_vault_kinds

    locked_kinds = [k for k, _ in systray.panel_rows({**base, "exists": True,
                                                      "locked": True})]
    assert "backup" in locked_kinds and "merge" not in locked_kinds

    unknown_kinds = [k for k, _ in systray.panel_rows({**base, "exists": True,
                                                       "locked": None})]
    assert "backup" in unknown_kinds and "merge" not in unknown_kinds

    open_kinds = [k for k, _ in systray.panel_rows({**base, "exists": True,
                                                    "locked": False})]
    assert "backup" in open_kinds and "merge" in open_kinds
