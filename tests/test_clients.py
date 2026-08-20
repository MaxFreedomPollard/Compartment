"""Writing into somebody else's configuration file is the dangerous part.

The tools that make Compartment reachable from Cursor or Zed or LM Studio are
not clever - they put one object into one JSON file. What makes them worth
testing is everything that must survive the edit: the other MCP servers the
user already had, the file when it will not parse, the entry when it is
already there. A memory server that eats somebody's Cursor config on the way
in has not integrated with anything.

Every case here starts from a config that already has another server in it,
because that is the state every real machine is in.
"""
import json

import pytest

from compartment import clients


VAULT = "/v/memory.vault"

#: What a user's config looks like before we touch it. If any test finishes
#: with this missing or changed, the writer is not safe to ship.
OTHER = {"command": "other-server", "args": ["--flag"]}


def cfg_with_other(path, root="mcpServers"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({root: {"someone-elses": OTHER},
                                "unrelatedSetting": 42}, indent=2))
    return path


# ------------------------------------------------------------------ the table

def test_every_client_resolves_a_path_without_raising():
    for key, c in clients.CLIENTS.items():
        p = c.config_path()
        assert str(p), f"{key} produced an empty path"


def test_every_alias_points_at_a_real_client():
    for alias, key in clients.ALIASES.items():
        assert clients.resolve(alias) is clients.CLIENTS[key]


def test_aliases_and_casing_are_accepted():
    assert clients.resolve("Roo-Code").key == "roo"
    assert clients.resolve("  GEMINI-CLI ").key == "gemini"
    assert clients.resolve("nothing-like-this") is None


def test_the_clients_we_will_not_write_say_so():
    # Cherry Studio keeps MCP config in application state and Goose keeps it
    # in YAML we would reformat. Both must be declared, not attempted.
    assert clients.CLIENTS["cherry-studio"].writes is False
    assert clients.CLIENTS["goose"].writes is False
    assert clients.CLIENTS["cursor"].writes is True


# ------------------------------------------------------------- the JSON write

def test_registering_keeps_every_other_server(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is True
    data = json.loads(p.read_text())
    assert data["mcpServers"]["someone-elses"] == OTHER
    assert data["unrelatedSetting"] == 42
    assert data["mcpServers"]["compartment"]["args"][:2] == ["--vault", VAULT]


def test_the_backup_is_byte_exact(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    before = p.read_bytes()
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["backup"], "no backup was taken"
    from pathlib import Path
    assert Path(res["backup"]).read_bytes() == before


def test_registering_twice_updates_rather_than_duplicates(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    clients.register(clients.CLIENTS["cursor"], VAULT, path=p)
    clients.register(clients.CLIENTS["cursor"], "/other.vault", path=p)

    servers = json.loads(p.read_text())["mcpServers"]
    assert len(servers) == 2                    # theirs and exactly one ours
    assert servers["compartment"]["args"][1] == "/other.vault"


def test_a_config_that_will_not_parse_is_left_alone(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"mcpServers": {  // a comment JSON does not allow\n}')
    before = p.read_text()

    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is False
    assert "refusing to touch it" in res["reason"]
    assert res["snippet"] and "compartment" in res["snippet"]
    assert p.read_text() == before, "a config we could not parse was modified"


def test_a_missing_config_is_created(tmp_path):
    p = tmp_path / "nested" / "mcp.json"
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is True
    assert res["backup"] is None                # nothing existed to back up
    assert "compartment" in json.loads(p.read_text())["mcpServers"]


def test_a_root_key_that_is_not_an_object_is_refused(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": ["not", "an", "object"]}))
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)
    assert res["written"] is False
    assert json.loads(p.read_text())["mcpServers"] == ["not", "an", "object"]


# ------------------------------------------------------- the shapes that vary

def test_vscode_uses_servers_and_names_the_transport(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json", root="servers")
    clients.register(clients.CLIENTS["vscode"], VAULT, path=p)

    data = json.loads(p.read_text())
    assert "mcpServers" not in data
    assert data["servers"]["someone-elses"] == OTHER
    assert data["servers"]["compartment"]["type"] == "stdio"


def test_zed_uses_context_servers(tmp_path):
    p = cfg_with_other(tmp_path / "settings.json", root="context_servers")
    clients.register(clients.CLIENTS["zed"], VAULT, path=p)

    data = json.loads(p.read_text())
    assert data["context_servers"]["someone-elses"] == OTHER
    assert "compartment" in data["context_servers"]


def test_opencode_takes_one_command_array(tmp_path):
    p = cfg_with_other(tmp_path / "opencode.json", root="mcp")
    clients.register(clients.CLIENTS["opencode"], VAULT, path=p)

    entry = json.loads(p.read_text())["mcp"]["compartment"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert isinstance(entry["command"], list)
    assert entry["command"][1:3] == ["--vault", VAULT]


def test_each_client_is_told_which_caller_it_is(tmp_path):
    p = tmp_path / "mcp.json"
    clients.register(clients.CLIENTS["lmstudio"], VAULT, path=p)
    args = json.loads(p.read_text())["mcpServers"]["compartment"]["args"]
    assert args[args.index("--caller") + 1] == "lmstudio"


# --------------------------------------------------------------------- TOML

def test_codex_gets_a_table_appended_and_keeps_the_rest(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# a comment somebody wrote\nmodel = "o3"\n\n'
                 '[mcp_servers.other]\ncommand = "other"\n')

    res = clients.register(clients.CLIENTS["codex"], VAULT, path=p)
    text = p.read_text()

    assert res["written"] is True
    assert "# a comment somebody wrote" in text     # comments survive
    assert '[mcp_servers.other]' in text            # their server survives
    assert '[mcp_servers.compartment]' in text
    assert f'"{VAULT}"' in text


def test_codex_is_not_registered_twice(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[mcp_servers.compartment]\ncommand = "compartment"\n')
    res = clients.register(clients.CLIENTS["codex"], VAULT, path=p)

    assert res["written"] is False
    assert p.read_text().count("[mcp_servers.compartment]") == 1


def test_codex_unregister_takes_only_our_table(tmp_path):
    p = tmp_path / "config.toml"
    clients.register(clients.CLIENTS["codex"], VAULT, path=p)
    p.write_text(p.read_text() + '\n[mcp_servers.other]\ncommand = "x"\n')

    assert clients.unregister(clients.CLIENTS["codex"], path=p) is True
    text = p.read_text()
    assert "[mcp_servers.compartment]" not in text
    assert '[mcp_servers.other]' in text
    assert 'command = "x"' in text


# ----------------------------------------------------------------- removal

def test_unregister_removes_only_ours(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert clients.unregister(clients.CLIENTS["cursor"], path=p) is True
    servers = json.loads(p.read_text())["mcpServers"]
    assert servers == {"someone-elses": OTHER}


def test_unregister_reports_when_we_were_never_there(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    assert clients.unregister(clients.CLIENTS["cursor"], path=p) is False


# ------------------------------------------------------- the ones we only print

@pytest.mark.parametrize("key", [k for k, c in clients.CLIENTS.items()
                                 if not c.writes])
def test_a_manual_client_writes_nothing_and_hands_back_a_block(key, tmp_path):
    p = tmp_path / "would-be-config"
    res = clients.register(clients.CLIENTS[key], VAULT, path=p)

    assert res["written"] is False
    assert not p.exists(), f"{key} wrote a file it was told not to write"
    assert "compartment" in res["snippet"]


def test_the_printed_block_is_valid_json_for_json_clients():
    c = clients.CLIENTS["continue"]
    parsed = json.loads(clients.snippet(c, VAULT))
    assert "compartment" in parsed[c.root]


def test_omp_resolves_and_writes_agent_level_mcp_json():
    c = clients.CLIENTS["omp"]
    assert c.writes is True
    entry = json.loads(clients.snippet(c, VAULT))["mcpServers"]["compartment"]
    assert entry["args"][:2] == ["--vault", VAULT]
    assert entry["args"][-1] == "serve"


# -------------------------------------------------------------------- status

def test_present_is_false_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(clients.Path, "home",
                        classmethod(lambda cls: tmp_path))
    assert clients.CLIENTS["cursor"].present() is False


def test_status_has_a_row_for_every_client():
    rows = clients.status()
    assert len(rows) == len(clients.CLIENTS)
    assert {r["key"] for r in rows} == set(clients.CLIENTS)


# ---------------------------------------------------------- the command itself

def test_the_written_command_is_an_absolute_path(tmp_path, monkeypatch):
    """A GUI client launched from the Dock inherits launchd's PATH, and an
    install that is only Compartment.app never put `compartment` on any PATH
    for it to inherit. A bare name there names nothing."""
    import sys
    binf = tmp_path / "bin"
    binf.mkdir()
    (binf / "compartment").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(clients.shutil, "which", lambda n: None)
    assert clients.executable() == str(binf / "compartment")


def test_the_written_command_is_never_the_app_launcher(tmp_path, monkeypatch):
    """Inside Compartment.app the launcher sits in MacOS/ and is called
    `Compartment`; macOS filesystems are case-insensitive, so looking beside
    the interpreter for "compartment" finds it. A client pointed there would
    start the menu bar app instead of a memory server."""
    import sys
    macos = tmp_path / "Compartment.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "compartment").write_text("#!/bin/sh\n", encoding="utf-8")
    (macos / "python3").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(macos / "python3"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere"))
    monkeypatch.setattr(clients.shutil, "which",
                        lambda n: "/usr/local/bin/compartment")
    assert clients.executable() == "/usr/local/bin/compartment"


def test_the_command_falls_back_to_path_then_to_the_bare_name(tmp_path,
                                                              monkeypatch):
    import sys
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(clients.shutil, "which", lambda n: "/opt/bin/compartment")
    assert clients.executable() == "/opt/bin/compartment"
    monkeypatch.setattr(clients.shutil, "which", lambda n: None)
    assert clients.executable() == "compartment"
