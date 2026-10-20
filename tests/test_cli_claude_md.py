"""The CLAUDE.md block goes where Claude Code reads its user CLAUDE.md."""
from compartment import cli


def test_the_block_follows_claude_config_dir(tmp_path, monkeypatch):
    """With CLAUDE_CONFIG_DIR set, Claude Code reads CLAUDE.md from there;
    the block went to ~/.claude/CLAUDE.md, which it never loads."""
    monkeypatch.delenv("CLAUDE_MD", raising=False)
    monkeypatch.setattr(cli.Path, "home",
                        classmethod(lambda cls: tmp_path / "home"))
    moved = tmp_path / "dotfiles" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    md = cli._write_managed_claude_md()
    assert md == moved / "CLAUDE.md"
    assert cli._CLAUDE_MD_BEGIN in md.read_text(encoding="utf-8")
    assert not (tmp_path / "home" / ".claude" / "CLAUDE.md").exists()
    assert cli._remove_managed_claude_md() == md


def test_claude_md_still_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "moved"))
    monkeypatch.setenv("CLAUDE_MD", str(tmp_path / "explicit.md"))
    assert cli._claude_md_path() == tmp_path / "explicit.md"
