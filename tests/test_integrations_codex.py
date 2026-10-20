"""Codex reads one TOML file, and refuses to start at all when it cannot
read it. Whatever `integrate codex` appends has to leave a document Codex
can read, with every other setting in it exactly as it was."""
import tomllib

import pytest

from compartment import clients

CODEX = clients.CLIENTS["codex"]


@pytest.mark.parametrize("vault", [
    "/Users/ana/Vaults \U0001F512/memory.vault",     # an emoji in a folder
    "/home/\U00020BB7\u5409/memory.vault",           # CJK Extension B
    "C:\\Users\\Jos\u00e9\\.compartment\\memory.vault",
    '/tmp/a "quoted"\tname\x7f/memory.vault',
])
def test_the_codex_table_reads_back_whatever_the_vault_path_holds(vault,
                                                                 tmp_path):
    """The values were escaped with json.dumps, which writes a character
    outside the Basic Multilingual Plane as a UTF-16 surrogate pair; TOML
    allows only scalar values in an escape, so a vault in a folder with an
    emoji in its name left config.toml unreadable and Codex would not
    start."""
    p = tmp_path / "config.toml"
    p.write_text('model = "o3"\n', encoding="utf-8")

    res = clients.register(CODEX, vault, path=p)

    assert res["written"] is True, res["reason"]
    doc = tomllib.loads(p.read_text(encoding="utf-8"))
    assert doc["model"] == "o3"
    assert doc["mcp_servers"]["compartment"]["args"][1] == vault
    assert tomllib.loads(clients.snippet(CODEX, vault))["mcp_servers"][
        "compartment"]["args"][1] == vault


@pytest.mark.parametrize("existing", [
    'mcp_servers = { other = { command = "x" } }\n',
    'mcp_servers = []\n',
    '[[mcp_servers]]\nname = "x"\ncommand = "y"\n',
])
def test_a_config_a_table_cannot_be_added_to_is_left_alone(existing,
                                                           tmp_path):
    """An inline `mcp_servers = { ... }` cannot be extended by a
    [mcp_servers.compartment] header, so the append turned a working config
    into one Codex could not read, every server in it gone with it."""
    p = tmp_path / "config.toml"
    p.write_text(existing, encoding="utf-8")

    res = clients.register(CODEX, "/v/memory.vault", path=p)

    assert res["written"] is False
    assert "refusing to touch it" in res["reason"]
    assert "[mcp_servers.compartment]" in res["snippet"]
    assert p.read_text(encoding="utf-8") == existing
    assert not p.with_name(p.name + clients.BACKUP_SUFFIX).exists()


def test_dotted_keys_for_another_server_still_take_the_table(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('mcp_servers.other.command = "x"\n', encoding="utf-8")
    res = clients.register(CODEX, "/v/memory.vault", path=p)
    assert res["written"] is True, res["reason"]
    servers = tomllib.loads(p.read_text(encoding="utf-8"))["mcp_servers"]
    assert servers["other"] == {"command": "x"} and "compartment" in servers


def _registered(p):
    return tomllib.loads(p.read_text(encoding="utf-8"))["mcp_servers"][
        "compartment"]


def test_registering_codex_again_refreshes_a_stale_entry(tmp_path,
                                                         monkeypatch):
    """Re-registering said "already registered; left as it is" whatever the
    entry held, so an entry naming a bare `compartment` that Codex could
    not find, a script an upgrade had moved, or another vault was never
    put right by running `integrate codex` again, which is what every JSON
    client does."""
    p = tmp_path / "config.toml"
    p.write_text(
        '# my settings\nmodel = "o3"\n\n'
        '[mcp_servers.compartment]\ncommand = "compartment"\n'
        'args = ["--vault", "/old.vault", "--caller", "codex", "serve"]\n'
        'startup_timeout_sec = 30\n\n'
        '[mcp_servers.compartment.env]\nCOMPARTMENT_EMBED_DAEMON = "0"\n\n'
        "# Bob's server\n[mcp_servers.bob]\ncommand = \"bob\"\n",
        encoding="utf-8")
    monkeypatch.setattr(clients, "executable", lambda: "/opt/bin/compartment")

    res = clients.register(CODEX, "/new.vault", path=p)

    assert res["written"] is True, res["reason"]
    text = p.read_text(encoding="utf-8")
    doc = tomllib.loads(text)
    ours = doc["mcp_servers"]["compartment"]
    assert ours["command"] == "/opt/bin/compartment"
    assert ours["args"][:2] == ["--vault", "/new.vault"]
    # What the user added to our table stays with it.
    assert ours["startup_timeout_sec"] == 30
    assert ours["env"] == {"COMPARTMENT_EMBED_DAEMON": "0"}
    assert doc["mcp_servers"]["bob"] == {"command": "bob"}
    assert doc["model"] == "o3"
    assert text.startswith("# my settings\n") and "# Bob's server" in text
    assert text.count("[mcp_servers.compartment]") == 1
    backup = p.with_name(p.name + clients.BACKUP_SUFFIX)
    assert "/old.vault" in backup.read_text(encoding="utf-8")

    # Refreshing again changes nothing and does not grow the file.
    again = clients.register(CODEX, "/new.vault", path=p)
    assert again["written"] is False
    assert again["reason"] == "already registered; left as it is"
    assert p.read_text(encoding="utf-8") == text
    monkeypatch.setattr(clients, "executable", lambda: "/usr/bin/compartment")
    clients.register(CODEX, "/new.vault", path=p)
    clients.register(CODEX, "/other.vault", path=p)
    after = p.read_text(encoding="utf-8")
    assert "\n\n\n" not in after
    assert _registered(p)["args"][1] == "/other.vault"


def test_a_stale_codex_entry_that_cannot_be_taken_out_is_left_for_the_user(
        tmp_path):
    p = tmp_path / "config.toml"
    text = ('[mcp_servers]\ncompartment = { command = "compartment", '
            'args = ["serve"] }\nother = { command = "x" }\n')
    p.write_text(text, encoding="utf-8")
    res = clients.register(CODEX, "/v/memory.vault", path=p)
    assert res["written"] is False
    assert "left as it is" in res["reason"]
    assert "[mcp_servers.compartment]" in res["snippet"]
    assert p.read_text(encoding="utf-8") == text
