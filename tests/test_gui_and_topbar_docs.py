"""Tests for top-bar/tray document search and the optional cross-platform GUI.

Covers:
* Read-only VaultView `doc_search` and `doc_get` (zero writes, zero audit rows,
  incremental journal catch-up, locked refusal, ASCII JSONL wire protocol).
* PanelState `search_documents` / `get_document`, `open_gui` / `stop_gui`, and
  PanelState.lock() stopping GUI helpers.
* Preserved macOS menubar & Windows/Linux systray controls + new document
  search and Open GUI surfaces (and Linux desktop action).
* Pure headless `GuiController` end-to-end (lifecycle, external lock key drop,
  external agent write sync, full DMS CRUD/versioning/diff/import/export/
  compile/shred, memory grounding & staleness, relations, settings, JSONL,
  audit verify).
* CLI `compartment gui --self-check` (and `app` alias), plus live Tk window
  build smoke test when a display is available and macOS offscreen popover
  render check.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from compartment import cli, gui, menubar, session, systray
from compartment.platforms import file_signature
from compartment.vault import Vault
from compartment.vaultview import VaultView, serve

from conftest import PASS


def _open_vault(vault_path: str) -> Vault:
    v = Vault.create(vault_path, PASS, creator="test")
    session.store(vault_path, v._master)
    return v


def _audit_count(vault_path: str) -> int:
    v = Vault.unlock(vault_path, passphrase=PASS)
    try:
        return v.db.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
    finally:
        v.lock()


# ------------------------------------------- VaultView read-only doc search

def test_vaultview_doc_search_and_get_are_read_only_and_catch_up(vault_path):
    v = _open_vault(vault_path)
    v.save()
    view = VaultView(vault_path)

    # Zero-document fast path
    empty = view.doc_search("taxes")
    assert empty["ok"] is True
    assert empty["results"] == []
    assert empty["total_documents"] == 0

    # Add a document via journal append while VaultView is already open
    d1 = v.doc_put(
        path="taxes/2026-return.md",
        title="2026 Tax Summary",
        content="# 2026 Taxes\n\n## Deductions\nHome office deduction is $4200.\n",
        tags=["finance", "taxes"],
        caller="user",
    )
    v.store(
        "Home office deduction for 2026 is $4200.",
        caller="user", source="doc:taxes/2026-return.md",
        doc_id=d1["id"], doc_section="Deductions",
    )

    # Verify incremental tail catch-up (before compaction)
    res_prefix = view.doc_search("tax")
    assert res_prefix["ok"] is True
    assert res_prefix["total_documents"] == 1
    assert res_prefix["results"][0]["id"] == d1["id"]
    assert res_prefix["read"] == "tail"

    # Compact cleanly, then verify strict read-only invariant (bytes + audit).
    # _audit_count opens/unlocks the vault, so take audit_before FIRST, then sig_before.
    v.save()
    v.lock()
    audit_before = _audit_count(vault_path)
    sig_before = file_signature(vault_path)

    res_body = view.doc_search("home office deduction")
    assert res_body["ok"] is True
    assert res_body["results"][0]["id"] == d1["id"]

    # Blank query lists recent documents
    res_list = view.doc_search("")
    assert res_list["ok"] is True
    assert res_list["results"][0]["path"] == "taxes/2026-return.md"

    # doc_get returns full preview + outline + grounded memories
    got = view.doc_get(d1["id"])
    assert got["ok"] is True
    doc = got["document"]
    assert doc["title"] == "2026 Tax Summary"
    assert any(h["heading"] == "Deductions" for h in doc["outline"])
    assert len(doc["memories"]) == 1

    # Strict read-only invariant: file bytes and audit chain untouched
    assert file_signature(vault_path) == sig_before
    assert _audit_count(vault_path) == audit_before

    # Patch document via journal -> VaultView catches up on tail & sees v2
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    assert view.doc_search("home office")["ok"] is True
    v2.doc_patch(d1["id"], append="\n## Filing\nFiled electronically.\n",
                 caller="user")
    res_v2 = view.doc_search("filed electronically")
    assert res_v2["ok"] is True
    assert res_v2["read"] == "tail"
    assert res_v2["results"][0]["version"] == 2
    got_v2 = view.doc_get(d1["id"])
    assert got_v2["document"]["memories"][0]["stale"] is True
    v2.lock()

    # Locking vault refuses document search and drops key
    session.clear(vault_path)
    locked_res = view.doc_search("taxes")
    assert locked_res["locked"] is True
    assert not view.holding


def test_vaultview_wire_protocol_supports_doc_search_and_doc_get(vault_path):
    v = _open_vault(vault_path)
    d = v.doc_put(
        path="specs/protocol.md", title="Protocol Spec",
        content="# Protocol\nUses UTF-8 café and AES-256.\n", caller="user")
    v.save()
    v.lock()

    lines = "\n".join([
        json.dumps({"op": "doc_search", "id": 11, "query": "protocol", "limit": 5}),
        json.dumps({"op": "doc_get", "id": 12, "doc_id": d["id"], "max_chars": 4000}),
    ]) + "\n"
    out = io.StringIO()
    assert serve(vault_path, stdin=io.StringIO(lines), out=out) == 0
    raw_out = out.getvalue()
    assert raw_out.isascii()
    replies = [json.loads(ln) for ln in raw_out.splitlines()]
    assert replies[0]["id"] == 11 and replies[0]["ok"] is True
    assert replies[0]["results"][0]["id"] == d["id"]
    assert replies[1]["id"] == 12 and replies[1]["ok"] is True
    assert "café" in replies[1]["document"]["content"]


# ------------------------------------------- Top bar & tray shared layer

def test_panel_state_document_search_and_gui_lifecycle(vault_path, monkeypatch):
    v = _open_vault(vault_path)
    d = v.doc_put(
        path="runbooks/deploy.md", title="Deploy Runbook",
        content="# Deploy\nRun canary first.\n", caller="user")
    v.save()
    v.lock()

    view = VaultView(vault_path)

    class _LocalView:
        def request(self, op, timeout=None, **fields):
            if op == "snapshot":
                return view.snapshot(limit=fields.get("limit", 5))
            if op == "doc_search":
                return view.doc_search(
                    fields.get("query", ""), limit=fields.get("limit", 6))
            if op == "doc_get":
                return view.doc_get(
                    fields.get("doc_id", ""),
                    max_chars=fields.get("max_chars", 24_000))
            return None

        def held(self):
            import contextlib
            return contextlib.nullcontext()

        def close(self):
            view.drop()

    ps = menubar.PanelState(vault_path, view=_LocalView())
    s_res = ps.search_documents("canary")
    assert s_res["ok"] is True
    assert s_res["results"][0]["id"] == d["id"]

    g_res = ps.get_document(d["id"])
    assert g_res["ok"] is True
    assert "canary" in g_res["document"]["content"]

    # open_gui / stop_gui lifecycle & PanelState.lock() stops spawned GUI
    spawned = []

    class _FakeProc:
        def __init__(self, argv):
            self.argv = argv
            self.pid = 99999
            self.alive = True
            self.terminated = False

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated = True
            self.alive = False

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kw):
        p = _FakeProc(list(argv))
        spawned.append(p)
        return p

    monkeypatch.setattr(menubar, "user_path",
                        lambda: os.environ.get("PATH", ""))
    monkeypatch.setattr(menubar.subprocess, "Popen", fake_popen)
    menubar.stop_gui()
    assert menubar.open_gui(vault_path) is True
    assert len(spawned) == 1
    assert "gui" in spawned[0].argv and vault_path in spawned[0].argv

    # Passing doc_id launches targeted open
    assert menubar.open_gui(vault_path, doc_id=d["id"]) is True
    assert len(spawned) == 2
    assert "--doc" in spawned[1].argv and d["id"] in spawned[1].argv

    # Locking through PanelState stops spawned GUI helpers
    monkeypatch.setattr(menubar, "stop_dashboard", lambda: None)
    assert ps.lock(lambda: True) is True
    assert all(p.terminated for p in spawned)
    assert ps.snapshot()["documents"] == 0


def test_systray_panel_rows_keep_all_options_and_add_doc_search_and_gui():
    unlocked = {
        "vault": "/v", "exists": True, "locked": False, "records": 42,
        "organic": 5, "documents": 3,
        "recent": [{"text": "one fact"}], "error": None,
        "settings": {"capture_hook": True, "search_starter_facts": True,
                     "expire_memories": True, "auto_lock_minutes": 30},
    }
    rows = systray.panel_rows(unlocked)
    kinds = [k for k, _ in rows]
    # Every original option preserved
    for req in ("state", "lock", "backup", "merge", "change",
                "toggle:capture_hook", "toggle:search_starter_facts",
                "toggle:expire_memories", "choice:auto_lock_minutes",
                "connect:claude", "memory"):
        assert req in kinds
    # New document search + Open GUI added when unlocked
    assert "doc_search" in kinds
    assert "gui" in kinds
    assert ("heading", "DOCUMENTS (3)") in rows

    # Locked vault hides document search
    locked = {**unlocked, "locked": True, "recent": []}
    locked_kinds = [k for k, _ in systray.panel_rows(locked)]
    assert "doc_search" not in locked_kinds
    assert "unlock" in locked_kinds


def test_linux_desktop_entry_adds_gui_action_without_breaking_panel(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    out = systray.install_desktop_entry("/home/u/test.vault")
    assert not out.startswith("error")
    text = systray.desktop_entry_path().read_text(encoding="utf-8")
    exec_lines = [ln for ln in text.splitlines() if ln.startswith("Exec=")]
    assert len(exec_lines) == 2
    assert "panel" in exec_lines[0] and "/home/u/test.vault" in exec_lines[0]
    assert "gui" in exec_lines[1] and "/home/u/test.vault" in exec_lines[1]
    assert "[Desktop Action gui]" in text


# ------------------------------------------- Optional GUI controller (headless)

def test_gui_controller_full_dms_memory_relation_and_security_lifecycle(
        tmp_path, monkeypatch):
    vp = str(tmp_path / "gui.vault")
    ctrl = gui.GuiController(vp)
    assert ctrl.exists is False and ctrl.is_open is False

    # Create vault directly and test unlock via passphrase
    v0 = Vault.create(vp, PASS, creator="test")
    v0.save()
    v0.lock()
    assert ctrl.exists is True

    ok, _ = ctrl.unlock("wrong-passphrase")
    assert ok is False and ctrl.is_open is False

    ok, msg = ctrl.unlock(PASS)
    assert ok is True and ctrl.is_open is True, msg

    # Save a persistent document (v1)
    saved = ctrl.save_doc(
        path="architecture/storage.md",
        title="Storage Architecture",
        content="# Storage\n\n## Engine\nSingle encrypted SQLite vault.\n",
        collection="specs",
        tags=["architecture", "core"],
        pinned=True,
        change_note="initial spec",
    )
    did = saved["id"]
    assert saved["version"] == 1

    # Ground an atomic memory in v1
    mem = ctrl.store_memory(
        "Compartment stores all encrypted data inside a single vault file.",
        source="doc:architecture/storage.md",
        kind="fact",
        tags=["architecture"],
        doc_id=did,
        doc_section="Engine",
    )
    assert mem["id"]

    # Patch document -> v2 flags grounded memory stale
    patched = ctrl.patch_doc(
        did,
        append="\n## Documents\nPer-version keys support crypto-shred.\n",
        change_note="add documents section",
    )
    assert patched["version"] == 2
    assert any(m["id"] == mem["id"] for m in patched["stale_memories"])

    # Diff v1 -> v2
    diff_out = ctrl.diff_doc(did, 1, 2)
    assert "+## Documents" in diff_out["diff"]

    # Verify list_docs filters (pinned, stale_only) and search_docs
    pinned = ctrl.list_docs(pinned_only=True)["documents"]
    assert [d["id"] for d in pinned] == [did]
    stale_docs = ctrl.list_docs(stale_only=True)["documents"]
    assert [d["id"] for d in stale_docs] == [did]
    stale_mems = ctrl.stale_memories()["stale"]
    assert any(m["id"] == mem["id"] for m in stale_mems)
    hits = ctrl.search_docs("crypto-shred")["results"]
    assert hits and hits[0]["id"] == did

    # Compile memories into a new living Markdown doc
    compiled = ctrl.compile_memories(
        "compiled/arch.md", title="Compiled Architecture", query="single vault")
    assert compiled["compiled_memories"] >= 1

    # Import file from disk & export document back to disk
    src_file = tmp_path / "note.txt"
    src_file.write_text("Imported quarterly plan details.\n", encoding="utf-8")
    imp = ctrl.import_path(str(src_file))
    assert imp["imported"] == 1
    exp_dest = tmp_path / "exported.md"
    exp = ctrl.export_doc(did, str(exp_dest))
    assert exp_dest.is_file() and "Per-version keys" in exp_dest.read_text(encoding="utf-8")
    exp_all_dir = tmp_path / "all_docs"
    exp_all = ctrl.export_all(str(exp_all_dir))
    assert exp_all["exported"] >= 3

    # Relations CRUD
    rel = ctrl.add_relation("Compartment", "stores", "documents", doc_id=did)
    rels = ctrl.list_relations(entity="Compartment")["relations"]
    assert any(r["id"] == rel["id"] for r in rels)
    assert ctrl.unlink_relation(rel["id"]) is True

    # Settings toggle including search_documents_in_memory_search
    s1 = ctrl.set_setting("search_documents_in_memory_search", False)
    assert s1["search_documents_in_memory_search"] is False
    s2 = ctrl.set_setting("search_documents_in_memory_search", True)
    assert s2["search_documents_in_memory_search"] is True

    # JSONL export with documents + audit verify + summary
    jpath = tmp_path / "dump.jsonl"
    jout = ctrl.export_jsonl(str(jpath), include_documents=True)
    assert jout["lines"] >= 3 and jpath.is_file()
    aud = ctrl.verify_audit()
    assert aud["ok"] is True
    exp_out = ctrl.expire_now()
    assert exp_out["expired"] == 0
    st = ctrl.summary()
    assert st["locked"] is False and st["documents"] >= 3

    # External agent write sync: another Vault instance writes a doc on disk
    ext = Vault.unlock(vp, passphrase=PASS)
    ext_doc = ext.doc_put(
        path="agent/written-externally.md",
        content="# Agent Note\nWritten by an MCP agent.\n", caller="agent")
    ext.lock()
    assert ctrl.sync_if_changed() is True
    assert ctrl.get_doc(ext_doc["id"])["path"] == "agent/written-externally.md"

    # External lock detection: clearing session drops in-memory key immediately
    session.clear(vp)
    assert ctrl.check_session_alive() is False
    assert ctrl.is_open is False

    # Re-unlock, archive & crypto-shred a doc, then lock cleanly
    assert ctrl.unlock(PASS)[0] is True
    ctrl.archive_doc(compiled["id"])
    assert ctrl.get_doc(compiled["id"])["status"] == "archived"
    del_res = ctrl.delete_doc(compiled["id"], shred=True)
    assert del_res["deleted"] is True and del_res["shredded"] is True

    ok_lock, _ = ctrl.lock()
    assert ok_lock is True and ctrl.is_open is False
    assert Vault.find_credential(vp) is None


def test_cli_gui_self_check_and_alias_work_offline(vault_path, capsys):
    v = _open_vault(vault_path)
    v.doc_put(path="notes/hello.md", content="# Hello\nWorld\n", caller="user")
    v.save()
    v.lock()

    with pytest.raises(SystemExit) as ex:
        cli.main(["--vault", vault_path, "gui", "--self-check"])
    assert ex.value.code == 0
    out = capsys.readouterr().out
    assert "documents: 1" in out
    assert "locked:    False" in out

    # Alias `app` routes to cmd_gui too
    with pytest.raises(SystemExit) as ex2:
        cli.main(["--vault", vault_path, "app", "--self-check"])
    assert ex2.value.code == 0


def test_tk_gui_window_builds_cleanly_when_display_available(vault_path):
    """Build the real Tk GUI window (withdrawn) in a clean child process so
    widget wiring, fonts, menus, and bindings are verified live without
    leaving Tk Variable objects to finalize on later tests' threads."""
    pytest.importorskip("tkinter")
    v = _open_vault(vault_path)
    d = v.doc_put(
        path="guide/start.md", title="Getting Started",
        content="# Guide\n\n## First Step\nOpen the GUI.\n", caller="user")
    v.save()
    v.lock()

    code = (
        "import sys, tkinter as tk\n"
        "from compartment import gui\n"
        "try:\n"
        "    root = tk.Tk()\n"
        "except tk.TclError:\n"
        "    sys.exit(77)\n"
        "root.withdraw()\n"
        f"ctrl = gui.GuiController({vault_path!r})\n"
        "app = None\n"
        "try:\n"
        f"    app = gui._build_window(root, ctrl, initial_doc_id={d['id']!r})\n"
        "    root.update()\n"
        "    assert ctrl.is_open is True\n"
        "    assert 'notebook' in app['widgets']\n"
        "    assert 'doc_search_entry' in app['widgets']\n"
        "    assert 'doc_editor' in app['widgets']\n"
        "finally:\n"
        "    if app and app.get('watch_id') is not None:\n"
        "        try:\n"
        "            root.after_cancel(app['watch_id'])\n"
        "        except Exception:\n"
        "            pass\n"
        "    ctrl.close()\n"
        "    root.destroy()\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    if res.returncode == 77:
        pytest.skip("no graphical display in this test environment")
    assert res.returncode == 0, res.stderr + res.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS AppKit popover render")
def test_macos_menubar_renders_popover_with_document_search(vault_path, tmp_path):
    """Offscreen PDF->PNG render of the live macOS popover confirms AppKit
    layout, document search field, and Open GUI button build cleanly."""
    pytest.importorskip("AppKit")
    v = _open_vault(vault_path)
    v.doc_put(path="notes/spec.md", title="Spec",
              content="# Spec\nHello\n", caller="user")
    v.save()
    v.lock()

    png = tmp_path / "popover.png"
    res = subprocess.run(
        [sys.executable, "-m", "compartment.cli", "--vault", vault_path,
         "menubar", "--render", str(png)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr + res.stdout
    assert png.is_file() and png.stat().st_size > 5_000
