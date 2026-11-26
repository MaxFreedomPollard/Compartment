"""Comprehensive tests for Compartment's encrypted Document Management System
(DMS) and how it strengthens core atomic memory without reducing it.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import pathlib
import sys
import threading
import types
import urllib.request
import zipfile
import zlib

import pytest

from compartment import cli, dash, documents, server, session, vaultfile
from compartment.acl import AclError
from compartment.crypto import CryptoError
from compartment.vault import Vault
from compartment.vaultview import VaultView

from conftest import PASS

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run_cli(monkeypatch, capsys, argv: list[str],
             stdin_text: str | None = None):
    if stdin_text is not None:
        raw = stdin_text.encode("utf-8")
        stream = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        monkeypatch.setattr(sys, "stdin", stream)
    else:
        monkeypatch.setattr("compartment.cli._can_prompt", lambda: False)
    cli.main(argv)
    return capsys.readouterr()


# ---------------------------------------------- paths, extraction, & helpers

def test_path_normalization_and_traversal_rejection():
    assert documents.normalize_doc_path("/taxes\\2026//summary.md") == "taxes/2026/summary.md"
    assert documents.normalize_doc_path(title="Q4 Architecture Spec!") == "documents/q4-architecture-spec.md"
    assert documents.folder_of("taxes/2026/summary.md") == "taxes/2026"
    assert documents.folder_of("readme.md") == ""
    for bad in ("../secret.md", "a/../../b.md", "a/./b.md", "a\x00b.md", "C:/win.txt", ""):
        with pytest.raises(CryptoError):
            documents.normalize_doc_path(bad)


def test_stdlib_text_extraction_across_formats():
    # HTML strips scripts/styles and preserves readable prose
    html_raw = b"<html><head><style>body{color:red}</style><script>alert(1)</script></head><body><h1>Lease</h1><p>Rent is 2400 monthly.</p></body></html>"
    html_txt = documents.extract_searchable_text(html_raw, "text/html", "lease.html")
    assert "Lease" in html_txt and "Rent is 2400 monthly." in html_txt
    assert "alert(1)" not in html_txt and "color:red" not in html_txt

    # Synthetic DOCX (zip + word/document.xml)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>C60 LLC Operating Agreement</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Delaware formation date 2025-04-01.</w:t></w:r></w:p></w:body></w:document>",
        )
    docx_txt = documents.extract_searchable_text(
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "agreement.docx",
    )
    assert "Operating Agreement" in docx_txt and "2025-04-01" in docx_txt

    # Synthetic ODT
    odt_buf = io.BytesIO()
    with zipfile.ZipFile(odt_buf, "w") as zf:
        zf.writestr(
            "content.xml",
            '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            "<office:body><office:text><text:p>OpenDocument Tax Note 7742</text:p></office:text></office:body>"
            "</office:document-content>",
        )
    assert "Tax Note 7742" in documents.extract_searchable_text(
        odt_buf.getvalue(), "application/vnd.oasis.opendocument.text", "note.odt")

    # Synthetic compressed PDF stream
    pdf_stream = zlib.compress(b"BT /F1 12 Tf (Invoice Number INV-998877) Tj ET")
    pdf_raw = (b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(pdf_stream)).encode()
               + b" /Filter /FlateDecode >>\nstream\n" + pdf_stream
               + b"\nendstream\nendobj\n%%EOF")
    pdf_txt = documents.extract_searchable_text(pdf_raw, "application/pdf", "inv.pdf")
    assert "INV-998877" in pdf_txt

    # Corrupt binary degrades gracefully to ""
    assert documents.extract_searchable_text(b"\x89PNG\r\n\x1a\ncorrupt", "image/png", "x.png") == ""


def test_outline_ignores_fenced_code_blocks_and_extracts_sections():
    md = (
        "# System Guide\n\n"
        "Intro paragraph.\n\n"
        "## Deployment\n\n"
        "```bash\n"
        "# not a markdown heading\n"
        "echo deploy\n"
        "```\n\n"
        "### Rollback\n\n"
        "Run rollback.sh immediately.\n\n"
        "## Security\n\n"
        "Use XChaCha20-Poly1305.\n"
    )
    outline = documents.extract_outline(md)
    assert [h["path"] for h in outline] == [
        "System Guide",
        "System Guide > Deployment",
        "System Guide > Deployment > Rollback",
        "System Guide > Security",
    ]
    sec = documents.extract_section(md, "Rollback")
    assert sec is not None and "rollback.sh" in sec["content"]
    assert "XChaCha20" not in sec["content"]


# ------------------------------ encryption, versioning, patching, & replay

def test_document_encryption_at_rest_versioning_patching_and_journal_replay(
        vault, vault_path):
    v1_text = (
        "# Vault Architecture\n\n"
        "## Storage\n\n"
        "All records use per-record XChaCha20-Poly1305 keys.\n\n"
        "## Backup Policy\n\n"
        "Weekly cold backups go to encrypted SSD vault-omega-91.\n"
    )
    r1 = vault.doc_put(
        path="specs/architecture.md",
        content=v1_text,
        collection="engineering",
        tags=["architecture", "crypto"],
        summary="Core vault encryption and backup architecture",
        pinned=True,
        change_note="initial spec",
        caller="user",
    )
    assert r1["created_new"] is True and r1["version"] == 1
    assert r1["title"] == "Vault Architecture"
    assert r1["pinned"] is True
    assert r1["chunks_indexed"] >= 2
    did = r1["id"]

    # Zero plaintext on disk (even in the uncompacted journal!)
    disk_bytes = open(vault_path, "rb").read()
    assert b"vault-omega-91" not in disk_bytes
    assert b"Vault Architecture" not in disk_bytes
    assert b"specs/architecture.md" not in disk_bytes

    # Identical content is deduplicated (no redundant version created)
    r_same = vault.doc_put(
        path="specs/architecture.md", content=v1_text, caller="user")
    assert r_same["unchanged"] is True and r_same["version"] == 1

    # Incremental section patch creates v2
    r2 = vault.doc_patch(
        "specs/architecture.md",
        section="Backup Policy",
        section_content="Daily cold backups go to dual SSDs vault-omega-91 and vault-sigma-42.",
        change_note="switch to daily dual-SSD backups",
        caller="user",
    )
    assert r2["version"] == 2 and r2["id"] == did

    # Exact find/replace + append creates v3
    r3 = vault.doc_patch(
        did,
        replace_old="per-record XChaCha20-Poly1305 keys",
        replace_new="per-record and per-document-version XChaCha20-Poly1305 keys",
        append="## Recovery\n\nVerify signatures with `compartment verify`.\n",
        change_note="document version keys and recovery section",
        caller="user",
    )
    assert r3["version"] == 3

    # Ambiguous replace fails loudly
    with pytest.raises(CryptoError, match="matched 2"):
        vault.doc_patch(did, replace_old="vault-", replace_new="box-", caller="user")

    # Journal replay without save(): reopen fresh from disk and verify everything!
    reopened = Vault.unlock(vault_path, passphrase=PASS)
    got_v3 = reopened.doc_get(
        "specs/architecture.md", include_history=True, diff_from_version=1,
        caller="user")
    assert got_v3["version"] == 3 and got_v3["requested_version"] == 3
    assert len(got_v3["versions"]) == 3
    assert "vault-sigma-42" in got_v3["content"]
    assert "compartment verify" in got_v3["content"]
    assert "-Weekly cold backups" in got_v3["diff"]
    assert "+Daily cold backups" in got_v3["diff"]

    # Historical version v1 still readable verbatim
    got_v1 = reopened.doc_get(did, version=1, caller="user")
    assert got_v1["requested_version"] == 1
    assert "Weekly cold backups" in got_v1["content"]
    assert "vault-sigma-42" not in got_v1["content"]

    # Section read, outline-only read, and windowed line slicing
    sec = reopened.doc_get(did, section="Recovery", caller="user")
    assert "compartment verify" in sec["content"]
    assert "Backup Policy" not in sec["content"]

    outline_doc = reopened.doc_get(did, outline_only=True, caller="user")
    assert "content" not in outline_doc
    assert any(h["heading"] == "Recovery" for h in outline_doc["outline"])

    sliced = reopened.doc_get(did, start_line=1, end_line=3, caller="user")
    assert sliced["slice"]["truncated"] is True
    assert sliced["slice"]["start_line"] == 1 and sliced["slice"]["end_line"] == 3

    # Metadata-only update + rename preserves doc_id and history
    renamed = reopened.doc_put(
        doc_id=did, new_path="architecture/vault-spec.md",
        tags=["architecture", "crypto", "core"], caller="user")
    assert renamed["id"] == did and renamed["path"] == "architecture/vault-spec.md"
    assert renamed["version"] == 3 and renamed["metadata_updated"] is True
    assert len(reopened.doc_history("architecture/vault-spec.md", caller="user")["versions"]) == 3


# ------------------ core synergy: grounding, staleness, & unified search

def test_document_grounding_staleness_detection_and_non_crowding_memory_search(
        vault):
    # Vault with 0 documents adds zero extra keys to memory_search
    m_plain = vault.store(
        "Office Wi-Fi SSID is CopperLantern-5G.",
        caller="user", source="from chat")
    plain_search = vault.search("CopperLantern Wi-Fi", caller="user")
    assert "documents" not in plain_search
    assert plain_search["results"][0]["id"] == m_plain["id"]
    assert "doc_id" not in plain_search["results"][0]

    # Store a persistent contract document
    doc = vault.doc_put(
        path="legal/office-lease.md",
        content=(
            "# Office Lease 2026\n\n"
            "## Financial Terms\n\n"
            "Monthly rent is $3,200 due on the 1st. Security deposit is $6,400.\n\n"
            "## Renewal Clause\n\n"
            "Tenant must give 60 days written notice before 2027-05-01.\n"
        ),
        collection="legal",
        tags=["lease", "office"],
        caller="user",
    )
    did = doc["id"]

    # Ground atomic memories & a graph relation in the document
    m1 = vault.store(
        "Office lease monthly rent is $3,200 due on the 1st.",
        caller="user", doc_id=did, doc_section="Financial Terms",
        tags=["lease"])
    assert m1["doc_id"] == did and m1["doc_version"] == 1
    assert m1["doc_path"] == "legal/office-lease.md"
    assert "doc:legal/office-lease.md (v1)" in m1["source"]
    assert not m1.get("doc_stale")

    m2 = vault.store(
        "Office lease renewal requires 60 days notice before 2027-05-01.",
        caller="user", source="lease review", doc_id="legal/office-lease.md",
        doc_section="Renewal Clause", tags=["lease"])
    assert m2["doc_id"] == did and m2["doc_version"] == 1

    rel = vault.link(
        "Office Lease", "requires_notice", "60 days",
        caller="user", src_id=m2["id"], doc_id=did)
    assert rel["doc_id"] == did

    # memory_search returns atomic memories in `results` (with grounding metadata)
    # AND matching document passages in separate `documents` array!
    res = vault.search("office lease monthly rent deposit", caller="user")
    mem_ids = [r["id"] for r in res["results"]]
    assert m1["id"] in mem_ids
    m1_hit = next(r for r in res["results"] if r["id"] == m1["id"])
    assert m1_hit["doc_id"] == did
    assert m1_hit["doc_path"] == "legal/office-lease.md"
    assert m1_hit["doc_version"] == 1
    assert m1_hit["doc_section"] == "Financial Terms"
    assert not m1_hit.get("doc_stale")
    assert "documents" in res and any(d["id"] == did for d in res["documents"])
    doc_hit = next(d for d in res["documents"] if d["id"] == did)
    assert "6,400" in doc_hit["excerpt"] or "3,200" in doc_hit["excerpt"]

    # Filtering memory_search by doc_id returns only memories grounded in that doc
    scoped = vault.search("office", caller="user", doc_id=did)
    assert all(r["doc_id"] == did for r in scoped["results"])
    assert m_plain["id"] not in [r["id"] for r in scoped["results"]]

    # include_documents=False omits the documents list cleanly
    no_docs = vault.search("office lease", caller="user", include_documents=False)
    assert "documents" not in no_docs

    # Now evolve the persistent document to v2 -> automatic staleness detection!
    upd = vault.doc_patch(
        did,
        section="Financial Terms",
        section_content="Monthly rent increased to $3,450 effective July 2026. Security deposit remains $6,400.",
        change_note="rent adjustment amendment",
        caller="user",
    )
    assert upd["version"] == 2
    assert len(upd["stale_memories"]) == 2
    assert {s["id"] for s in upd["stale_memories"]} == {m1["id"], m2["id"]}

    # Stale flag propagates across memory_get, memory_search, doc_get, doc_list, status, and doc_stale_memories
    g1 = vault.get(m1["id"], caller="user")
    assert g1["doc_stale"] is True and g1["doc_current_version"] == 2

    s_after = vault.search("monthly rent", caller="user")
    m1_after = next(r for r in s_after["results"] if r["id"] == m1["id"])
    assert m1_after["doc_stale"] is True and m1_after["doc_current_version"] == 2

    stale_report = vault.doc_stale_memories(caller="user")
    assert stale_report["count"] == 2

    stale_docs = vault.doc_list(caller="user", stale_memories_only=True)
    assert [d["id"] for d in stale_docs["documents"]] == [did]
    assert stale_docs["documents"][0]["stale_memory_count"] == 2

    st = vault.status()
    assert st["documents"] == 1
    assert st["document_versions"] == 2
    assert st["grounded_memories"] == 2
    assert st["stale_grounded_memories"] == 2

    # Updating (superseding) m1 against v2 drops stale count to 1
    m1_v2 = vault.store(
        "Office lease monthly rent is $3,450 effective July 2026.",
        caller="user", doc_id=did, doc_section="Financial Terms",
        supersedes=[m1["id"]])
    assert m1_v2["doc_version"] == 2 and not m1_v2.get("doc_stale")
    assert vault.status()["stale_grounded_memories"] == 1

    # Bidirectional navigation: doc_memories shows live memories + relations
    dm = vault.doc_memories(did, caller="user")
    assert len(dm["memories"]) == 2
    assert len(dm["relations"]) == 1 and dm["relations"][0]["predicate"] == "requires_notice"


# ----------------------- binary docs, crypto-shred, archive, & ACLs

def test_binary_documents_archive_version_prune_and_crypto_shred(
        vault, vault_path, tmp_path):
    raw_png = b"\x89PNG\r\n\x1a\n" + b"BINARY-SECRET-RECEIPT-8841" + os.urandom(512)
    doc = vault.doc_put(
        path="receipts/2026/hardware.png",
        content_base64=base64.b64encode(raw_png).decode(),
        title="Mac Studio Hardware Receipt",
        collection="finance",
        tags=["receipt", "hardware"],
        extracted_text="Apple Store receipt total $4,199 order W991234567 Mac Studio M4 Max.",
        caller="user",
    )
    did = doc["id"]
    assert doc["is_binary"] is True and doc["mime_type"] == "image/png"

    # Default doc_get omits binary base64 to protect agent context windows
    g_default = vault.doc_get(did, caller="user")
    assert g_default["binary_omitted"] is True
    assert "content_base64" not in g_default
    assert "W991234567" in g_default["extracted_text"]

    # Explicit include_binary=True returns exact bytes
    g_bin = vault.doc_get(did, include_binary=True, caller="user")
    assert base64.b64decode(g_bin["content_base64"]) == raw_png

    # Search finds the binary receipt via its extracted text!
    hits = vault.doc_search("W991234567 Mac Studio", caller="user")["results"]
    assert hits and hits[0]["id"] == did

    # Save v2 so we can test historical version deletion
    raw_png_v2 = raw_png + b"UPDATED-STAMP"
    vault.doc_put(
        doc_id=did, raw_bytes=raw_png_v2,
        extracted_text="Apple Store updated receipt W991234567 with AppleCare.",
        caller="user")

    # Export v1 and v2 back to disk byte-identically
    exp_v1 = tmp_path / "out_v1.png"
    exp_v2 = tmp_path / "out_v2.png"
    vault.doc_export_file(did, str(exp_v1), version=1, caller="user")
    vault.doc_export_file(did, str(exp_v2), caller="user")
    assert exp_v1.read_bytes() == raw_png
    assert exp_v2.read_bytes() == raw_png_v2

    # Delete historical v1 with shred; current v2 cannot be deleted alone
    with pytest.raises(CryptoError, match="Cannot delete current version"):
        vault.doc_delete(did, version=2, caller="user")
    d_v1 = vault.doc_delete(did, version=1, shred=True, caller="user")
    assert d_v1["version_deleted"] == 1 and d_v1["shredded"] is True
    assert len(vault.doc_history(did, caller="user")["versions"]) == 1

    # Archive hides from default active list/search, visible under status='archived'/'all'
    mem = vault.store("Mac Studio order number is W991234567.",
                      caller="user", doc_id=did)
    arch = vault.doc_delete(did, archive=True, caller="user")
    assert arch["archived"] is True and mem["id"] in arch["affected_memory_ids"]
    assert vault.doc_list(caller="user")["total"] == 0
    assert vault.doc_list(caller="user", status="archived")["total"] == 1
    assert vault.doc_search("W991234567", caller="user")["results"] == []
    assert len(vault.doc_search("W991234567", caller="user", status="all")["results"]) == 1
    assert vault.get(mem["id"], caller="user")["doc_stale"] is True

    # Permanent crypto-shred wipes everything and keeps the grounded memory intact
    shred = vault.doc_delete(did, shred=True, caller="user")
    assert shred["deleted"] is True and shred["shredded"] is True
    assert mem["id"] in shred["affected_memory_ids"]
    assert vault.db.document_count() == 0
    assert vault.doc_search("W991234567", caller="user", status="all")["results"] == []
    m_after = vault.get(mem["id"], caller="user")
    assert m_after["doc_missing"] is True and m_after["doc_stale"] is True
    assert b"W991234567" not in open(vault_path, "rb").read()
    assert b"BINARY-SECRET-RECEIPT-8841" not in open(vault_path, "rb").read()


def test_document_acls_enforce_ro_and_none(vault):
    vault.config.callers["reader"] = {
        "default_namespace": "work",
        "grants": {"work": "ro", "secret": "none"},
    }
    vault.config.callers["outsider"] = {
        "default_namespace": "public",
        "grants": {"public": "rw", "work": "none", "secret": "none"},
    }
    wdoc = vault.doc_put(
        path="runbooks/deploy.md", content="Deploy steps for cluster-77.",
        namespace="work", caller="user")
    sdoc = vault.doc_put(
        path="keys/root.md", content="Top secret master vault procedure.",
        namespace="secret", caller="user")

    # ro caller can read/list/search work, cannot write work, cannot see secret
    assert vault.doc_get(wdoc["id"], caller="reader")["id"] == wdoc["id"]
    assert [d["id"] for d in vault.doc_list(caller="reader")["documents"]] == [wdoc["id"]]
    assert [d["id"] for d in vault.doc_search("cluster-77", caller="reader")["results"]] == [wdoc["id"]]
    with pytest.raises(AclError):
        vault.doc_patch(wdoc["id"], append="unauthorized edit", caller="reader")
    with pytest.raises(AclError):
        vault.doc_delete(wdoc["id"], caller="reader")
    with pytest.raises(AclError):
        vault.doc_get(sdoc["id"], caller="reader")
    assert vault.doc_search("Top secret master", caller="reader")["results"] == []

    # outsider sees neither namespace
    assert vault.doc_list(caller="outsider")["documents"] == []
    assert vault.status(caller="outsider")["documents"] == 0


# ----------------- merge, reembed, export/import, compile, & directory sync

def test_lossless_merge_reembed_jsonl_roundtrip_and_dir_sync(
        tmp_path, monkeypatch):
    path_a = str(tmp_path / "a.vault")
    path_b = str(tmp_path / "b.vault")
    va = Vault.create(path_a, PASS, creator="user")
    d_shared = va.doc_put(
        path="notes/roadmap.md", content="# Roadmap\n\nQ1: ship DMS.\n",
        tags=["plan"], caller="user")
    va.save()
    Vault.backup(path_a, path_b)

    # Machine B updates shared doc to v2, adds a new doc + grounded memory + relation
    vb = Vault.unlock(path_b, passphrase=PASS)
    vb.doc_patch(
        d_shared["id"], append="Q2: mobile sync.\n",
        change_note="add Q2", caller="user")
    d_new = vb.doc_put(
        path="tax/2026/ein.md", content="# Tax IDs\n\nFederal EIN is 99-1234567.\n",
        collection="finance", tags=["tax"], pinned=True, caller="user")
    m_b = vb.store(
        "Federal EIN is 99-1234567.", caller="user", doc_id=d_new["id"],
        doc_section="Tax IDs")
    vb.link("C60", "has_ein", "99-1234567", caller="user",
            src_id=m_b["id"], doc_id=d_new["id"])
    vb.save()
    del vb

    # Same-model merge reuses document chunk vectors with zero ONNX calls!
    def _no_embed(*_a, **_k):
        raise AssertionError("same-model merge must copy doc chunk vectors without ONNX")

    monkeypatch.setattr(
        va, "_embedder",
        type("_NoEmbed", (), {"embed_record": _no_embed, "embed_passages": _no_embed})())
    res = va.merge(path_b)
    assert res["reembedded"] is False
    assert res["documents_imported"] == 1
    assert res["documents_updated"] == 1
    assert res["document_versions_added"] == 2

    reopened = Vault.unlock(path_a, passphrase=PASS)
    assert reopened.doc_get("notes/roadmap.md", caller="user")["version"] == 2
    ein_doc = reopened.doc_get("tax/2026/ein.md", include_memories=True, caller="user")
    assert ein_doc["pinned"] is True and len(ein_doc["memories"]) == 1
    assert "99-1234567" in ein_doc["memories"][0]["text"]
    hits = reopened.doc_search("99-1234567 Federal EIN", caller="user")["results"]
    assert hits and hits[0]["path"] == "tax/2026/ein.md"

    # Re-embed migrates both records and document chunks cleanly
    reopened.reembed(caller="user")
    hits_after_reembed = reopened.doc_search("99-1234567", caller="user")["results"]
    assert hits_after_reembed and hits_after_reembed[0]["path"] == "tax/2026/ein.md"

    # Full JSONL export + import round-trip (documents + grounded memories + relations)
    dump = reopened.export_jsonl(
        caller="user", include_seeded=False,
        include_relations=True, include_documents=True)
    path_c = str(tmp_path / "c.vault")
    vc = Vault.create(path_c, PASS, creator="user")
    vc.import_jsonl(dump, caller="user")
    ein_c = vc.doc_get("tax/2026/ein.md", include_memories=True, caller="user")
    assert len(ein_c["memories"]) == 1
    assert "99-1234567" in ein_c["memories"][0]["text"]
    assert len(vc.doc_history("notes/roadmap.md", caller="user")["versions"]) == 2

    # Compile memories into a living Markdown dossier
    compiled = vc.doc_compile(
        "dossiers/tax-summary.md", title="Tax Dossier",
        query="Federal EIN", caller="user")
    assert compiled["compiled_memories"] >= 1
    assert "99-1234567" in vc.doc_get("dossiers/tax-summary.md", caller="user")["content"]

    # Directory import (idempotent) + export-all round-trip
    src_dir = tmp_path / "folder_in"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "a.md").write_text("# Alpha\nFirst note.\n", encoding="utf-8")
    (src_dir / "sub" / "b.txt").write_text("Beta text file.\n", encoding="utf-8")
    (src_dir / ".hidden").write_text("skip me", encoding="utf-8")
    imp1 = vc.doc_import_path(str(src_dir), prefix="imported", caller="user")
    assert imp1["imported"] == 2 and imp1["unchanged"] == 0
    imp2 = vc.doc_import_path(str(src_dir), prefix="imported", caller="user")
    assert imp2["imported"] == 0 and imp2["unchanged"] == 2
    (src_dir / "a.md").write_text("# Alpha\nRevised second note.\n", encoding="utf-8")
    imp3 = vc.doc_import_path(str(src_dir), prefix="imported", caller="user")
    assert imp3["updated"] == 1 and imp3["unchanged"] == 1

    exp_dir = tmp_path / "folder_out"
    exp = vc.doc_export_all(str(exp_dir), path_prefix="imported", caller="user")
    assert exp["exported"] == 2
    assert "Revised second note." in (exp_dir / "imported" / "a.md").read_text(encoding="utf-8")
    assert "Beta text file." in (exp_dir / "imported" / "sub" / "b.txt").read_text(encoding="utf-8")


# ---------------- MCP server, CLI, Dashboard, VaultView, & Hermes end-to-end

def test_mcp_server_document_tools_and_batch_grounding(monkeypatch, vault, vault_path):
    import time
    for key, value in (("vault", vault), ("path", vault_path),
                       ("caller", "agent"), ("auto_lock_min", 30),
                       ("cred_sig", session.watch_signature(vault_path)),
                       ("cred_checked", time.monotonic())):
        monkeypatch.setitem(server._state, key, value)

    d = json.loads(server.doc_put.__wrapped__(
        path="projects/apollo.md",
        content="# Apollo\n\n## Stack\n\nUses Rust 1.85 and SQLite.\n",
        tags=["apollo"], collection="projects"))
    assert d["version"] == 1

    # Batch memory_store_many grounded in one doc_id
    batch = json.loads(server.memory_store_many.__wrapped__(
        facts=[
            {"text": "Project Apollo backend uses Rust 1.85.", "doc_section": "Stack"},
            {"text": "Project Apollo stores state in SQLite.", "doc_section": "Stack"},
        ],
        source="apollo spec",
        doc_id=d["id"],
    ))
    assert batch["count"] == 2
    assert all(r["doc_id"] == d["id"] and r["doc_version"] == 1 for r in batch["stored"])

    # Invalid doc_id in batch refuses whole batch before writing anything
    with pytest.raises(server.MemoryToolError):
        server.memory_store_many.__wrapped__(
            facts=[{"text": "Should never be stored."}],
            source="test",
            doc_id="nonexistent-doc-id",
        )
    assert vault.db.count() == 2

    # doc_patch + doc_get + doc_list + doc_search + doc_delete through MCP
    p = json.loads(server.doc_patch.__wrapped__(
        doc=d["id"], section="Stack",
        section_content="Uses Rust 1.88 and SQLite with WAL.",
        change_note="bump rust"))
    assert p["version"] == 2 and len(p["stale_memories"]) == 2

    g = json.loads(server.doc_get.__wrapped__(
        doc="projects/apollo.md", include_memories=True, diff_from_version=1))
    assert "Rust 1.88" in g["content"] and "+Uses Rust 1.88" in g["diff"]
    assert all(m["stale"] is True for m in g["memories"])

    ls = json.loads(server.doc_list.__wrapped__(collection="projects", stale_memories_only=True))
    assert ls["total"] == 1

    sr = json.loads(server.doc_search.__wrapped__(query="Rust 1.88 SQLite"))
    assert sr["results"] and sr["results"][0]["id"] == d["id"]

    # memory_search surfaces both atomic memories and matching documents
    ms = json.loads(server.memory_search.__wrapped__(query="Apollo Rust SQLite"))
    assert ms["results"] and ms["documents"]

    dl = json.loads(server.doc_delete.__wrapped__(doc=d["id"], archive=True))
    assert dl["archived"] is True


def test_cli_doc_subcommands_end_to_end(tmp_path, monkeypatch, capsys):
    vp = str(tmp_path / "cli_doc.vault")
    v = Vault.create(vp, PASS, creator="user")
    session.store(vp, v._master)
    v.lock()
    try:
        out = _run_cli(monkeypatch, capsys, [
            "--vault", vp, "doc", "put", "guides/runbook.md",
            "--content", "# Runbook\n\n## Restart\n\nRun systemctl restart api.\n",
            "--collection", "ops", "--tag", "ops", "--pin", "--json",
        ]).out
        doc = json.loads(out)
        assert doc["version"] == 1 and doc["pinned"] is True

        _run_cli(monkeypatch, capsys, [
            "--vault", vp, "store", "API service restarts via systemctl restart api.",
            "--source", "runbook", "--doc-id", doc["id"], "--doc-section", "Restart",
        ])
        _run_cli(monkeypatch, capsys, [
            "--vault", vp, "doc", "patch", "guides/runbook.md",
            "--section", "Restart", "--content", "Run systemctl reload api gracefully.",
            "--change-note", "prefer reload",
        ])
        stale_out = json.loads(_run_cli(monkeypatch, capsys, [
            "--vault", vp, "doc", "stale", "--json",
        ]).out)
        assert stale_out["count"] == 1

        diff_out = _run_cli(monkeypatch, capsys, [
            "--vault", vp, "doc", "diff", "guides/runbook.md", "1", "2",
        ]).out
        assert "-Run systemctl restart api." in diff_out
        assert "+Run systemctl reload api gracefully." in diff_out

        search_out = json.loads(_run_cli(monkeypatch, capsys, [
            "--vault", vp, "doc", "search", "systemctl reload", "--json",
        ]).out)
        assert search_out["results"] and search_out["results"][0]["path"] == "guides/runbook.md"

        hist_out = json.loads(_run_cli(monkeypatch, capsys, [
            "--vault", vp, "doc", "history", "guides/runbook.md", "--json",
        ]).out)
        assert len(hist_out["versions"]) == 2
    finally:
        session.clear(vp)


def test_vaultview_catches_up_on_journaled_documents(vault_path):
    v = Vault.create(vault_path, PASS, creator="user")
    session.store(vault_path, v._master)
    v.save()
    try:
        view = VaultView(vault_path)
        snap0 = view.snapshot()
        assert snap0["ok"] and snap0["documents"] == 0

        d = v.doc_put(path="notes/live.md", content="Journaled live doc.", caller="user")
        snap1 = view.snapshot()
        assert snap1["read"] == "tail" and snap1["documents"] == 1

        v.doc_delete(d["id"], archive=True, caller="user")
        snap2 = view.snapshot()
        assert snap2["read"] == "tail" and snap2["documents"] == 0
    finally:
        session.clear(vault_path)


def test_dashboard_document_endpoints_are_read_only(vault, vault_path):
    if os.environ.get("COMPARTMENT_ASSERT_OFFLINE") == "1":
        pytest.skip("dashboard binds loopback socket; exempt from offline guard")
    d = vault.doc_put(
        path="specs/dash.md",
        content="# Dashboard Spec\n\nServes documents strictly from RAM.\n",
        collection="specs", caller="user")
    vault.store("Dashboard serves strictly from RAM.", caller="user", doc_id=d["id"])
    vault.save()
    audit_before = vault.db.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]

    ref = dash._VaultRef(vault_path, vault)
    httpd, url = dash.start(ref)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(url + "api/stats", timeout=5) as r:
            stats = json.loads(r.read())
            assert r.headers["Cache-Control"] == "no-store"
        assert stats["documents"] == 1 and stats["grounded_memories"] == 1

        with urllib.request.urlopen(url + "api/documents", timeout=5) as r:
            docs = json.loads(r.read())
        assert docs["total"] == 1 and docs["documents"][0]["id"] == d["id"]

        with urllib.request.urlopen(url + f"api/doc?id={d['id']}", timeout=5) as r:
            doc_resp = json.loads(r.read())
        assert "Serves documents strictly from RAM." in doc_resp["content"]
        assert len(doc_resp["memories"]) == 1

        with urllib.request.urlopen(url + "api/doc_search?q=strictly%20RAM", timeout=5) as r:
            sr = json.loads(r.read())
        assert sr["results"] and sr["results"][0]["id"] == d["id"]
    finally:
        httpd.shutdown()
        httpd.server_close()

    audit_after = vault.db.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
    assert audit_after == audit_before


def test_hermes_provider_document_tools(monkeypatch, vault_path):
    agent = types.ModuleType("agent")
    base = types.ModuleType("agent.memory_provider")
    base.MemoryProvider = object
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", base)
    plugin_path = ROOT / "src" / "compartment" / "data" / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_dms_test", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("COMPARTMENT_VAULT", vault_path)

    v = Vault.create(vault_path, PASS, creator="test")
    session.store(vault_path, v._master)
    v.lock()
    provider = mod.CompartmentMemoryProvider()
    try:
        provider.initialize("s1")
        names = {s["name"] for s in provider.get_tool_schemas()}
        assert {"compartment_doc_put", "compartment_doc_get",
                "compartment_doc_search", "compartment_doc_list"} <= names

        put = json.loads(provider.handle_tool_call("compartment_doc_put", {
            "path": "notes/hermes-plan.md",
            "content": "# Hermes Plan\n\nUse encrypted documents as durable source.\n",
            "collection": "plans",
        }))
        assert put["version"] == 1
        st = json.loads(provider.handle_tool_call("compartment_store", {
            "text": "Hermes uses encrypted documents as durable source.",
            "doc_id": put["id"],
        }))
        assert st["doc_id"] == put["id"]
        got = json.loads(provider.handle_tool_call("compartment_doc_get", {
            "doc": put["id"], "include_memories": True,
        }))
        assert len(got["memories"]) == 1
        sr = json.loads(provider.handle_tool_call("compartment_doc_search", {
            "query": "encrypted documents durable source",
        }))
        assert sr["results"] and sr["results"][0]["id"] == put["id"]
        ls = json.loads(provider.handle_tool_call("compartment_doc_list", {}))
        assert ls["total"] == 1
    finally:
        session.clear(vault_path)
