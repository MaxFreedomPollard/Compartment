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



import pytest  # noqa: E402


@pytest.mark.parametrize("platform, words", [
    ("win32", "notification area"), ("darwin", "Cmd-Q"), ("linux", "quit")])
def test_the_desktop_hint_says_to_quit_it_not_close_it(platform, words):
    """"Restart Claude Desktop" read as closing its window, which leaves it
    running (in the notification area on Windows): it never rereads its
    config, and a copy still running can write the old one back."""
    hint = cli._quit_claude_desktop(platform)
    assert hint.startswith("Quit Claude Desktop completely") and words in hint


def test_connecting_claude_desktop_prints_that_hint(tmp_path, monkeypatch,
                                                    capsys):
    from compartment import claude_desktop
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(claude_desktop, "present", lambda: True)
    monkeypatch.setattr(claude_desktop, "register", lambda exe, args: {
        "config": str(tmp_path / "claude_desktop_config.json"),
        "backup": None})
    monkeypatch.setenv("CLAUDE_MD", str(tmp_path / "CLAUDE.md"))
    cli._integrate_target(cli.argparse.Namespace(
        target="claude", vault=str(tmp_path / "v.vault"), no_import=True,
        no_hooks=True))
    assert cli._quit_claude_desktop() in capsys.readouterr().out
