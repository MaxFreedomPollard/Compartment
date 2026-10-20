"""CLAUDE_CONFIG_DIR moves everything Claude Code keeps in ~/.claude:
settings.json, the per-project memory under projects/, skills/. People set
it to run a second account, or to keep the directory in a dotfiles tree
under a name that is not .claude."""
import pytest

from compartment import agent_skill, claude_hooks, claude_memory


@pytest.fixture()
def config_dir(monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR set, as it is in the environment Claude Code and
    `compartment` are started with. SETTINGS and DEFAULT_ROOT are worked out
    when used; a value an earlier test's monkeypatch left assigned on its
    way out would pin them, so none is left in place for this one."""
    d = tmp_path / "dotfiles" / "claude-work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    for module, name in ((claude_memory, "DEFAULT_ROOT"),
                         (claude_hooks, "SETTINGS")):
        if name in vars(module):
            monkeypatch.delattr(module, name)
    return d


def test_the_hook_goes_into_the_settings_claude_code_reads(config_dir):
    """The hook was installed into ~/.claude/settings.json whatever
    CLAUDE_CONFIG_DIR said, a file that Claude Code then never read."""
    assert claude_hooks.SETTINGS == config_dir / "settings.json"
    claude_hooks.install(compartment_bin="compartment")
    assert claude_hooks.is_installed() is True
    assert (config_dir / "settings.json").is_file()


def test_the_import_finds_the_memories_where_claude_code_keeps_them(
        config_dir):
    mem = config_dir / "projects" / "-home-ana-site" / "memory"
    mem.mkdir(parents=True)
    (mem / "likes-tea.md").write_text("---\nname: likes-tea\n---\nAna likes "
                                      "tea.\n", encoding="utf-8")
    (mem / claude_memory.INDEX_NAME).write_text("- likes-tea\n",
                                                encoding="utf-8")
    assert claude_memory.DEFAULT_ROOT == config_dir / "projects"
    assert claude_memory.discover() == [mem / "likes-tea.md"]


def test_the_hook_captures_memory_written_under_the_config_dir(config_dir):
    """is_memory_file wanted a `.claude` directory in the path, so with the
    config directory named anything else every memory write was passed
    over."""
    note = config_dir / "projects" / "-home-ana-site" / "memory" / "a.md"
    assert claude_hooks.is_memory_file(note) is True
    assert claude_hooks.is_memory_file(
        config_dir / "projects" / "-home-ana-site" / "notes.md") is False
    assert claude_hooks.is_memory_file(
        config_dir.parent / "elsewhere" / "memory" / "a.md") is False


def test_the_skill_goes_where_claude_code_loads_skills(config_dir):
    assert agent_skill.skill_path("claude") == (
        config_dir / "skills" / "compartmentalize" / "SKILL.md")


@pytest.mark.parametrize("value", ["", "   ", "relative/dir"])
def test_an_empty_or_relative_config_dir_is_ignored(value, monkeypatch,
                                                    tmp_path):
    monkeypatch.setattr(claude_memory.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    assert claude_memory.claude_config_dir() == tmp_path / ".claude"


def test_a_config_dir_under_home_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/claude-alt")
    assert claude_memory.claude_config_dir() == tmp_path / "claude-alt"
