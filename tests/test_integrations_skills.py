"""Where each agent loads the /compartmentalize skill from."""
import pytest

from compartment import agent_skill


@pytest.fixture()
def no_openclaw_env(monkeypatch):
    for var in ("OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_openclaw_home_stands_in_for_the_home_directory(no_openclaw_env,
                                                       monkeypatch,
                                                       tmp_path):
    """OPENCLAW_HOME replaces the home directory and OpenClaw keeps
    .openclaw under it (src/infra/config-dir.ts). It was taken for the
    .openclaw directory itself, so the skill landed one level above the
    skills/ OpenClaw loads."""
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "h"))
    assert agent_skill.skill_path("openclaw") == (
        tmp_path / "h" / ".openclaw" / "skills" / "compartmentalize"
        / "SKILL.md")


def test_openclaw_state_dir_and_config_path_move_it(no_openclaw_env,
                                                    monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH",
                       str(tmp_path / "cfg" / "openclaw.json"))
    assert agent_skill.agent_home("openclaw") == tmp_path / "cfg"
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    assert agent_skill.agent_home("openclaw") == tmp_path / "state"


def test_openclaw_without_any_variable_is_in_the_home_directory(
        no_openclaw_env, monkeypatch, tmp_path):
    monkeypatch.setitem(agent_skill.SKILL_TARGETS, "openclaw",
                        (agent_skill._openclaw_dir, tmp_path / ".openclaw"))
    assert agent_skill.agent_home("openclaw") == tmp_path / ".openclaw"
    r = agent_skill.install("openclaw")
    assert r["action"] == "written"
    assert agent_skill.is_current("openclaw")
    assert agent_skill.remove("openclaw") is True


def test_a_relative_claude_config_dir_does_not_scatter_the_skill(
        monkeypatch, tmp_path):
    """The hook and the import ignore a CLAUDE_CONFIG_DIR that is not
    absolute, as Claude Code's own VS Code extension does; the skill must
    not land relative to whatever directory the command ran in."""
    monkeypatch.setitem(agent_skill.SKILL_TARGETS, "claude",
                        (agent_skill._claude_dir, tmp_path / ".claude"))
    monkeypatch.setattr(agent_skill.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/claude")
    assert agent_skill.agent_home("claude") == tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "abs"))
    assert agent_skill.agent_home("claude") == tmp_path / "abs"
