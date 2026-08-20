"""Claude Desktop has to be wired, not described.

`integrate claude` used to print a JSON block and leave the pasting to the
user, so the one command whose whole job is connecting an agent finished with
Claude Desktop connected to nothing. These cover the writing that replaced it,
including the cases where writing would be the wrong move.
"""
import json
import os
import sys

import pytest

from compartment import claude_desktop


ENTRY_ARGS = ["--vault", "/v/memory.vault", "--caller", "claude-desktop",
              "serve"]


# ------------------------------------------------------------------ location

def test_the_config_path_is_the_apps_own_per_platform(
        monkeypatch, tmp_path, real_claude_desktop_config_path):
    monkeypatch.setattr(claude_desktop.Path, "home",
                        classmethod(lambda cls: tmp_path))
    p = real_claude_desktop_config_path()
    assert p.name == "claude_desktop_config.json"
    assert p.parent.name == "Claude"
    if sys.platform == "darwin":
        assert p.parent.parent.name == "Application Support"


@pytest.mark.skipif(os.name == "nt", reason="POSIX layout")
def test_linux_honours_xdg_config_home(monkeypatch, tmp_path,
                                       real_claude_desktop_config_path):
    if sys.platform == "darwin":
        pytest.skip("macOS uses Application Support")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert real_claude_desktop_config_path() == (
        tmp_path / "cfg" / "Claude" / "claude_desktop_config.json")


# ------------------------------------------------------------------ writing

def test_registering_creates_the_file_and_its_directories(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    out = claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)
    assert out["backup"] is None
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["compartment"] == {
        "command": "compartment", "args": ENTRY_ARGS}


def test_registering_keeps_everyone_elses_servers_and_backs_up(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"someone-else": {"command": "theirs"}},
        "unrelatedSetting": True}))
    out = claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)

    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["someone-else"] == {"command": "theirs"}
    assert data["unrelatedSetting"] is True
    assert "compartment" in data["mcpServers"]
    assert json.loads(open(out["backup"]).read())["mcpServers"] == {
        "someone-else": {"command": "theirs"}}


def test_registering_twice_updates_in_place(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    claude_desktop.register("compartment", ["old"], path=cfg)
    claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)
    servers = json.loads(cfg.read_text())["mcpServers"]
    assert list(servers) == ["compartment"]
    assert servers["compartment"]["args"] == ENTRY_ARGS


def test_a_malformed_config_is_refused_rather_than_guessed_at(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{ this is not json")
    with pytest.raises(ValueError):
        claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)
    assert cfg.read_text() == "{ this is not json"      # left untouched


def test_an_empty_file_is_not_malformed(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("")
    claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)
    assert "compartment" in json.loads(cfg.read_text())["mcpServers"]


# ------------------------------------------------------------------ reading

def test_is_registered_reports_what_is_actually_there(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    assert claude_desktop.is_registered(cfg) is False
    claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)
    assert claude_desktop.is_registered(cfg) is True


def test_a_malformed_config_reads_as_not_registered(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{ this is not json")
    assert claude_desktop.is_registered(cfg) is False


def test_present_is_false_when_the_app_is_nowhere(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_desktop.sys, "platform", "linux")
    assert claude_desktop.present(tmp_path / "gone" / "cfg.json") is False


def test_present_is_true_once_the_app_has_a_directory(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    assert claude_desktop.present(cfg) is True


def test_unregister_removes_only_ours(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps(
        {"mcpServers": {"someone-else": {"command": "theirs"}}}))
    claude_desktop.register("compartment", ENTRY_ARGS, path=cfg)
    assert claude_desktop.unregister(cfg) is True
    assert json.loads(cfg.read_text())["mcpServers"] == {
        "someone-else": {"command": "theirs"}}
    assert claude_desktop.unregister(cfg) is False


def test_the_suite_never_writes_the_real_claude_desktop_config(
        tmp_path, real_claude_desktop_config_path):
    """A guard on the guard.

    `integrate claude` writes Claude Desktop's config, and for several
    releases the suite wrote the developer's own - pointing a real
    installation at a pytest temp vault that no longer existed by the time
    the run finished. Nothing failed, and nothing said so.
    """
    from compartment import claude_desktop
    here = claude_desktop.config_path()
    assert here != real_claude_desktop_config_path()
    assert "pytest" in str(here)
