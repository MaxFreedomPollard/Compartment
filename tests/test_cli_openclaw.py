"""`integrate openclaw` and `uninstall` against the config current OpenClaw
reads: servers under `mcp.servers`, the file found by OpenClaw's own rule
for OPENCLAW_STATE_DIR, OPENCLAW_CONFIG_PATH and OPENCLAW_HOME."""
import json
import subprocess
import types

import pytest

from compartment import cli, menubar

VAULT = "/v/memory.vault"


@pytest.fixture()
def openclaw_home(tmp_path, monkeypatch):
    for name in ("OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    d = tmp_path / ".openclaw"
    d.mkdir()
    return d / "openclaw.json"


def _integrate():
    cli._integrate_target(types.SimpleNamespace(target="openclaw",
                                                vault=VAULT))


def test_the_server_goes_under_mcp_servers_and_an_old_entry_moves_there(
        openclaw_home, capsys):
    """OpenClaw reads `mcp.servers`, and its root schema rejects keys it does
    not know: the top-level `mcpServers` integrate wrote was never loaded,
    and could keep the whole config from loading."""
    openclaw_home.write_text(json.dumps({
        "gateway": {"port": 18789},
        "mcp": {"servers": {"theirs": {"command": "x"}}},
        "mcpServers": {"compartment": {"command": "old"}}}),
        encoding="utf-8")
    _integrate()
    cfg = json.loads(openclaw_home.read_text(encoding="utf-8"))
    assert "mcpServers" not in cfg
    servers = cfg["mcp"]["servers"]
    assert set(servers) == {"theirs", "compartment"}
    assert servers["compartment"]["args"] == ["--vault", VAULT, "--caller",
                                              "openclaw", "serve"]
    assert cfg["gateway"] == {"port": 18789}
    assert menubar.integration_status(VAULT)["openclaw"] is True


def test_the_block_to_paste_has_the_current_shape(openclaw_home, capsys):
    openclaw_home.write_text("[]\n", encoding="utf-8")
    _integrate()
    out = capsys.readouterr().out
    block = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert "compartment" in block["mcp"]["servers"]


@pytest.mark.parametrize("var", ["OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH"])
def test_openclaws_own_variables_name_the_config(tmp_path, monkeypatch, var):
    """OPENCLAW_HOME was taken for the .openclaw folder itself, one level too
    high, and the two variables that move the folder were not read."""
    d = tmp_path / "state"
    monkeypatch.setenv(var, str(d if var == "OPENCLAW_STATE_DIR"
                                else d / "openclaw.json"))
    assert menubar._openclaw_config() == d / "openclaw.json"


def test_uninstall_takes_it_out_of_both_places(openclaw_home, monkeypatch,
                                               capsys):
    openclaw_home.write_text(json.dumps({
        "mcp": {"servers": {"compartment": {}, "theirs": {}}},
        "mcpServers": {"compartment": {}, "also-theirs": {}}}),
        encoding="utf-8")
    monkeypatch.setattr(cli, "_tray_app",
                        lambda: (_ for _ in ()).throw(RuntimeError("no app")))
    monkeypatch.setattr(menubar, "_is_generated", lambda path: False)
    monkeypatch.setattr(subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 1, "", ""))
    for name in ("unregister", "is_registered"):
        monkeypatch.setattr(cli.clients, name, lambda *a, **k: False)
    monkeypatch.setattr(cli.claude_desktop, "unregister", lambda: False)
    monkeypatch.setattr(cli.claude_hooks, "is_installed", lambda: False)
    monkeypatch.setattr(cli.agent_skill, "SKILL_TARGETS", {})
    monkeypatch.setattr(cli, "_remove_managed_claude_md", lambda: None)
    cli.cmd_uninstall(types.SimpleNamespace(purge=False, vault=VAULT))
    cfg = json.loads(openclaw_home.read_text(encoding="utf-8"))
    assert cfg == {"mcp": {"servers": {"theirs": {}}},
                   "mcpServers": {"also-theirs": {}}}
    assert "OpenClaw registration removed" in capsys.readouterr().out
