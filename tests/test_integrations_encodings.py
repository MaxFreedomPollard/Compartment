"""Config files as Windows editors save them.

Notepad before Windows 10 1903 and PowerShell 5's `Out-File -Encoding utf8`
put a UTF-8 byte order mark at the front of the file; PowerShell 5's `>` and
plain `Out-File` write UTF-16. The Claude Desktop and Cursor guides send
Windows users to exactly those tools, so these are real files on real
machines, and each of them has another server in it that must survive.
"""
import codecs
import json
import tomllib

import pytest

from compartment import claude_desktop, claude_hooks, clients

VAULT = r"C:\Users\Ana\.compartment\memory.vault"
THEIRS = {"mcpServers": {"theirs": {"command": "npx", "args": ["-y", "x"]}},
          "globalShortcut": "Ctrl+Space"}

ENCODINGS = {
    "utf-8 with a mark (Notepad, Out-File -Encoding utf8)":
        lambda t: codecs.BOM_UTF8 + t.encode("utf-8"),
    "utf-16 (PowerShell 5's > and Out-File)":
        lambda t: t.encode("utf-16"),              # BOM + little-endian
    "utf-16 big-endian with a mark":
        lambda t: codecs.BOM_UTF16_BE + t.encode("utf-16-be"),
    "utf-8 with CRLF line endings":
        lambda t: t.replace("\n", "\r\n").encode("utf-8"),
}


def _write(path, text, how):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ENCODINGS[how](text)
    path.write_bytes(raw)
    return raw


def _is_plain_utf8(raw: bytes) -> bool:
    return not raw.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE,
                               codecs.BOM_UTF16_BE)) and b"\r\r" not in raw


@pytest.mark.parametrize("how", sorted(ENCODINGS))
def test_a_client_config_saved_by_a_windows_editor_is_registered(how,
                                                                 tmp_path):
    """The mark is not whitespace to json.loads, so a marked file was
    refused as "not valid JSON", and a UTF-16 one did not decode at all:
    the user was handed a block to paste into the file just refused."""
    p = tmp_path / ".cursor" / "mcp.json"
    before = _write(p, json.dumps(THEIRS, indent=2) + "\n", how)

    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is True, res["reason"]
    raw = p.read_bytes()
    assert _is_plain_utf8(raw)
    data = json.loads(raw.decode("utf-8"))
    assert data["mcpServers"]["theirs"] == THEIRS["mcpServers"]["theirs"]
    assert data["globalShortcut"] == "Ctrl+Space"
    assert data["mcpServers"]["compartment"]["args"][1] == VAULT
    assert open(res["backup"], "rb").read() == before    # byte-exact
    assert clients.is_registered(clients.CLIENTS["cursor"], path=p)
    assert clients.unregister(clients.CLIENTS["cursor"], path=p) is True


@pytest.mark.parametrize("how", sorted(ENCODINGS))
def test_claude_desktops_config_saved_by_a_windows_editor_is_registered(
        how, tmp_path):
    """Claude Desktop's own guide has Windows users open the file in
    Notepad. Its reader refused a marked file, and a UTF-16 one escaped
    as a bare UnicodeDecodeError."""
    p = tmp_path / "Claude" / claude_desktop.FILENAME
    _write(p, json.dumps(THEIRS, indent=2) + "\n", how)

    claude_desktop.register(r"C:\x\compartment.exe", ["serve"], path=p)

    raw = p.read_bytes()
    assert _is_plain_utf8(raw)
    data = json.loads(raw.decode("utf-8"))
    assert set(data["mcpServers"]) == {"theirs", "compartment"}
    assert data["mcpServers"]["compartment"]["command"] == (
        r"C:\x\compartment.exe")
    assert claude_desktop.is_registered(path=p) is True
    assert claude_desktop.unregister(path=p) is True


@pytest.mark.parametrize("how", sorted(ENCODINGS))
def test_claude_code_settings_saved_by_a_windows_editor_get_the_hook(
        how, tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    _write(p, json.dumps({"permissions": {"allow": ["Bash"]}}) + "\n", how)

    claude_hooks.install(compartment_bin=r"C:\x\compartment.exe",
                         settings=p)

    raw = p.read_bytes()
    assert _is_plain_utf8(raw)
    data = json.loads(raw.decode("utf-8"))
    assert data["permissions"] == {"allow": ["Bash"]}
    assert claude_hooks.is_installed(settings=p) is True
    assert claude_hooks.uninstall(settings=p) is True


@pytest.mark.parametrize("how", sorted(ENCODINGS))
def test_a_codex_config_saved_by_a_windows_editor_is_registered(how,
                                                                tmp_path):
    """Codex's TOML parser skips a leading mark; tomllib does not, so a
    config Codex reads happily was refused as "not valid TOML"."""
    p = tmp_path / ".codex" / "config.toml"
    _write(p, '# mine\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n',
           how)
    codex = clients.CLIENTS["codex"]

    res = clients.register(codex, VAULT, path=p)

    assert res["written"] is True, res["reason"]
    raw = p.read_bytes()
    assert _is_plain_utf8(raw)
    doc = tomllib.loads(raw.decode("utf-8"))
    assert doc["model"] == "o3" and doc["mcp_servers"]["other"] == {
        "command": "x"}
    assert doc["mcp_servers"]["compartment"]["args"][1] == VAULT
    text = raw.decode("utf-8")
    assert text.replace("\r\n", "\n").startswith("# mine\n")
    # The file keeps the line endings it had, on every system.
    assert ("\r\n" in text) == ("CRLF" in how)
    assert clients.is_registered(codex, path=p) is True
    assert clients.unregister(codex, path=p) is True
    assert "compartment" not in tomllib.loads(
        p.read_bytes().decode("utf-8"))["mcp_servers"]


def test_a_file_that_is_not_text_in_any_encoding_is_still_refused(tmp_path):
    p = tmp_path / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_bytes(b"\x00\xff\xfe\x80 not text")
    before = p.read_bytes()
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)
    assert res["written"] is False and "refusing to touch it" in res["reason"]
    assert p.read_bytes() == before
