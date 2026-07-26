"""Importing Claude Code's file memories, and the recency view.

compartment is only the source of truth if what the agent already learned comes
with it (the import) and the user can see that memory is alive (recent).
"""
import json

import pytest

from compartment import claude_memory
from compartment.crypto import CryptoError

MEM = """---
name: six-forks-housing
description: Furnished 1BR under $1,500 near Six Forks
metadata:
  type: project
---

Max wants a furnished one-bedroom under $1,500 a month, six-month lease OK,
walkable to retail.
"""

FEEDBACK = """---
name: answer-in-full
description: Answer with complete detail
metadata:
  type: feedback
---

Brevity reads as evasive; give itemized answers.
"""

NO_FRONTMATTER = "A bare note with no frontmatter at all.\n"


@pytest.fixture()
def memdir(tmp_path):
    """A Claude Code project layout: <root>/<project>/memory/*.md."""
    d = tmp_path / "projects" / "-Users-someone-Desktop" / "memory"
    d.mkdir(parents=True)
    (d / "six-forks-housing.md").write_text(MEM, encoding="utf-8")
    (d / "answer-in-full.md").write_text(FEEDBACK, encoding="utf-8")
    (d / "bare-note.md").write_text(NO_FRONTMATTER, encoding="utf-8")
    (d / "MEMORY.md").write_text("- [index](six-forks-housing.md) - hook\n",
                                 encoding="utf-8")
    return tmp_path / "projects"


def test_discover_skips_the_index(memdir):
    names = [p.name for p in claude_memory.discover(memdir)]
    assert "MEMORY.md" not in names          # a table of contents, not a fact
    assert sorted(names) == ["answer-in-full.md", "bare-note.md",
                             "six-forks-housing.md"]


def test_discover_accepts_a_memory_dir_directly(memdir):
    direct = memdir / "-Users-someone-Desktop" / "memory"
    assert len(claude_memory.discover(direct)) == 3


def test_discover_missing_root_is_empty(tmp_path):
    assert claude_memory.discover(tmp_path / "nope") == []


def test_parse_maps_frontmatter_to_tags_and_importance(memdir):
    rec = claude_memory.parse(memdir / "-Users-someone-Desktop" / "memory" /
                              "six-forks-housing.md")
    assert rec["name"] == "six-forks-housing"
    assert rec["tags"] == ["claude-memory", "project", "six-forks-housing"]
    assert rec["importance"] == claude_memory.IMPORTANCE["project"]
    # description and body both retrievable from the one record
    assert "Furnished 1BR" in rec["text"] and "walkable to retail" in rec["text"]
    assert "---" not in rec["text"]           # frontmatter stripped


def test_parse_feedback_outranks_project(memdir):
    d = memdir / "-Users-someone-Desktop" / "memory"
    fb = claude_memory.parse(d / "answer-in-full.md")
    pr = claude_memory.parse(d / "six-forks-housing.md")
    assert fb["importance"] > pr["importance"]


def test_parse_survives_missing_frontmatter(memdir):
    rec = claude_memory.parse(memdir / "-Users-someone-Desktop" / "memory" /
                              "bare-note.md")
    assert rec["name"] == "bare-note"
    assert rec["text"].startswith("A bare note")
    assert rec["importance"] == claude_memory.DEFAULT_IMPORTANCE


def test_import_stores_and_is_recallable(vault, memdir):
    res = claude_memory.import_files(vault, claude_memory.discover(memdir),
                                     caller="test")
    assert (res["imported"], res["failed"]) == (3, 0)
    hits = vault.search("where does Max want to live", caller="test",
                        top_k=3)["results"]
    assert any("1,500" in h["text"] or "furnished" in h["text"].lower()
               for h in hits)


def test_reimport_is_a_no_op(vault, memdir):
    files = claude_memory.discover(memdir)
    claude_memory.import_files(vault, files, caller="test")
    n = vault.db.count()
    again = claude_memory.import_files(vault, files, caller="test")
    assert again["imported"] == 0 and again["duplicates"] == 3
    assert vault.db.count() == n          # safe on every `integrate claude`


def test_import_never_modifies_the_source_files(vault, memdir):
    files = claude_memory.discover(memdir)
    before = {f: f.read_bytes() for f in files}
    claude_memory.import_files(vault, files, caller="test")
    assert all(f.read_bytes() == b for f, b in before.items())


def test_dry_run_writes_nothing(vault, memdir):
    res = claude_memory.import_files(None, claude_memory.discover(memdir),
                                     dry_run=True)
    assert res["imported"] == 3 and res["dry_run"] is True
    assert vault.db.count() == 0


def test_pending_counts_what_is_not_in_the_vault_yet(vault, memdir):
    assert claude_memory.pending(vault, memdir) == 3
    claude_memory.import_files(vault, claude_memory.discover(memdir),
                               caller="test")
    assert claude_memory.pending(vault, memdir) == 0


# ------------------------------------------------------------------ recent

def test_recent_returns_newest_last(vault):
    for t in ("first thing", "second thing", "third thing"):
        vault.store(t, caller="test")
    out = vault.recent(caller="test")
    assert [r["text"] for r in out["results"]] == ["first thing",
                                                   "second thing",
                                                   "third thing"]
    assert out["counts"]["organic"] == 3


def test_recent_limit_keeps_the_newest(vault):
    for i in range(5):
        vault.store(f"memory number {i}", caller="test")
    out = vault.recent(caller="test", limit=2)
    assert [r["text"] for r in out["results"]] == ["memory number 3",
                                                   "memory number 4"]


def test_recent_hides_seeded_facts_by_default(seeded_vault):
    seeded_vault.store("an organic memory", caller="test")
    hidden = seeded_vault.recent(caller="test", limit=50)
    assert [r["text"] for r in hidden["results"]] == ["an organic memory"]
    assert hidden["counts"]["seeded"] > 1000
    shown = seeded_vault.recent(caller="test", limit=50, include_seeded=True)
    assert len(shown["results"]) == 50      # starter facts drown it out


def test_recent_reports_created_timestamp(vault):
    vault.store("timestamped", caller="test")
    rec = vault.recent(caller="test")["results"][0]
    assert rec["created"] and rec["created_local"]
    assert rec["seeded"] is False


def test_recent_on_empty_vault(vault):
    out = vault.recent(caller="test")
    assert out["results"] == []
    assert out["counts"] == {"total": 0, "organic": 0, "seeded": 0}


def test_status_separates_organic_from_seeded(seeded_vault):
    # the seeded vault is shared across tests, so assert on the delta
    before = seeded_vault.status()["organic_records"]
    seeded_vault.store("a memory only this test stores", caller="test")
    st = seeded_vault.status()
    assert st["organic_records"] == before + 1
    assert st["seeded_records"] == st["records"] - st["organic_records"]
    assert st["seeded_records"] > 1000        # the starter pack, unchanged


def test_recent_requires_an_open_vault(vault):
    vault.lock()
    with pytest.raises(CryptoError):
        vault.recent(caller="test")


# --------------------------------------------- the install path itself
# `compartment integrate claude` imports on every run; that hook is the whole
# "switch to compartment on install" promise, so it is tested, not assumed.

def _prepared_vault(tmp_path, memdir, monkeypatch):
    from compartment import claude_memory as cm, session
    from compartment.vault import Vault
    monkeypatch.setattr(cm, "DEFAULT_ROOT", memdir)
    vp = str(tmp_path / "install.vault")
    v = Vault.create(vp, PASS_LOCAL, creator="test")
    session.store(vp, v._master)          # what `compartment init` leaves behind
    v.lock()
    return vp


PASS_LOCAL = "CorrectHorse"


def test_integrate_imports_existing_memories(tmp_path, memdir, monkeypatch,
                                             capsys):
    from compartment import cli
    from compartment.vault import Vault
    vp = _prepared_vault(tmp_path, memdir, monkeypatch)
    cli._migrate_claude_memories(vp)
    assert Vault.unlock(vp, passphrase=PASS_LOCAL).status()["organic_records"] == 3
    assert "3 existing Claude Code memories" in capsys.readouterr().out


def test_integrate_import_is_idempotent(tmp_path, memdir, monkeypatch):
    from compartment import cli
    from compartment.vault import Vault
    vp = _prepared_vault(tmp_path, memdir, monkeypatch)
    cli._migrate_claude_memories(vp)
    cli._migrate_claude_memories(vp)          # re-running integrate is safe
    assert Vault.unlock(vp, passphrase=PASS_LOCAL).status()["organic_records"] == 3


def test_integrate_no_import_flag_skips(tmp_path, memdir, monkeypatch, capsys):
    from compartment import cli
    from compartment.vault import Vault
    vp = _prepared_vault(tmp_path, memdir, monkeypatch)
    cli._migrate_claude_memories(vp, skip=True)
    assert Vault.unlock(vp, passphrase=PASS_LOCAL).status()["organic_records"] == 0
    assert "skipped (--no-import)" in capsys.readouterr().out


def test_integrate_pluralises_a_single_memory(tmp_path, monkeypatch, capsys):
    """One file must read 'memory', not 'memories', in BOTH branches."""
    from compartment import cli
    d = tmp_path / "one" / "proj" / "memory"
    d.mkdir(parents=True)
    (d / "solo.md").write_text(NO_FRONTMATTER, encoding="utf-8")
    vp = _prepared_vault(tmp_path, tmp_path / "one", monkeypatch)
    cli._migrate_claude_memories(vp, skip=True)
    out = capsys.readouterr().out
    assert "1 existing Claude Code memory found" in out
    cli._migrate_claude_memories(vp)
    assert "1 existing Claude Code memory into" in capsys.readouterr().out


def test_integrate_with_no_memories_is_silent(tmp_path, monkeypatch, capsys):
    from compartment import cli, claude_memory as cm
    monkeypatch.setattr(cm, "DEFAULT_ROOT", tmp_path / "empty")
    cli._migrate_claude_memories(str(tmp_path / "nope.vault"))
    assert capsys.readouterr().out == ""


def test_integrate_on_locked_vault_says_so(tmp_path, memdir, monkeypatch,
                                           capsys):
    """A locked vault must fail loudly with the fix, never silently skip."""
    from compartment import cli, claude_memory as cm, session
    from compartment.vault import Vault
    monkeypatch.setattr(cm, "DEFAULT_ROOT", memdir)
    vp = str(tmp_path / "locked.vault")
    Vault.create(vp, PASS_LOCAL, creator="test").lock()
    session.clear(vp)                          # no stored credential
    cli._migrate_claude_memories(vp)
    assert "locked" in capsys.readouterr().out


def test_mcp_instructions_claim_precedence():
    """The handshake must tell the model compartment replaces file memory -
    without it, a built-in memory declared in the host's system prompt wins."""
    from compartment.server import COMPARTMENT_INSTRUCTIONS as t
    assert "SUPERSEDES" in t
    assert "memory_store" in t and "MEMORY.md" in t


def test_claude_md_block_claims_precedence():
    from compartment.cli import _CLAUDE_MD_BODY as t
    assert "REPLACES" in t and "do not write new memories there" in t
