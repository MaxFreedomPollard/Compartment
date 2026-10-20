"""Claude Code's auto memory, wherever Claude Code keeps it."""
import json

import pytest

from compartment import claude_hooks, claude_memory


@pytest.fixture()
def claude_home(tmp_path, monkeypatch):
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)
    monkeypatch.setattr(claude_memory, "DEFAULT_ROOT", root)
    return tmp_path / ".claude"


def _settings(claude_home, **values):
    (claude_home / "settings.json").write_text(json.dumps(values),
                                               encoding="utf-8")


def _note(d, name, body="Ana prefers tea."):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"---\nname: {p.stem}\ndescription: x\nmetadata:\n  type: "
                 f"user\n---\n{body}\n", encoding="utf-8")
    return p


def test_memory_in_the_auto_memory_directory_is_found_and_captured(
        claude_home, tmp_path):
    """autoMemoryDirectory (Claude Code 2.1.74) moves auto memory out of
    projects/<project>/memory to any directory. Notes written there were
    invisible to `import-claude` and to the capture hook."""
    custom = tmp_path / "notes" / "claude"
    _settings(claude_home, autoMemoryDirectory=str(custom))
    moved = _note(custom, "tea.md")
    (custom / claude_memory.INDEX_NAME).write_text("- tea\n",
                                                   encoding="utf-8")
    usual = _note(claude_home / "projects" / "p" / "memory", "old.md")

    assert claude_memory.auto_memory_dir() == custom
    assert claude_memory.discover() == [usual, moved]
    assert claude_hooks.is_memory_file(moved) is True
    assert claude_hooks.is_memory_file(custom / claude_memory.INDEX_NAME) is (
        False)
    assert claude_hooks.is_memory_file(tmp_path / "notes" / "x.md") is False


def test_an_auto_memory_directory_under_home_is_expanded(claude_home,
                                                         tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _settings(claude_home, autoMemoryDirectory="~/mem")
    assert claude_memory.auto_memory_dir() == tmp_path / "mem"


@pytest.mark.parametrize("value", [None, "", "relative/mem", 3])
def test_no_or_unusable_auto_memory_directory_changes_nothing(claude_home,
                                                              value):
    _settings(claude_home, **({} if value is None
                              else {"autoMemoryDirectory": value}))
    assert claude_memory.auto_memory_dir() is None


def test_a_note_saved_with_a_byte_order_mark_keeps_its_frontmatter(
        tmp_path):
    """The mark hid the opening ---, so the frontmatter was not recognised
    and the whole block was stored as the fact, tagged with nothing."""
    p = tmp_path / "memory" / "tea.md"
    p.parent.mkdir()
    p.write_bytes(b"\xef\xbb\xbf---\nname: tea\ndescription: Ana likes tea"
                  b"\nmetadata:\n  type: user\n---\nGreen, no sugar.\n")
    rec = claude_memory.parse(p)
    assert rec["name"] == "tea" and "user" in rec["tags"]
    assert "---" not in rec["text"] and "﻿" not in rec["text"]
