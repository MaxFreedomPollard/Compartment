"""Clients that read their config as JSON with comments.

Zed writes settings.json on first run with a block of // comments above the
opening brace, so a strict reader refused every Zed user's file. VS Code's
mcp.json is JSON with comments too. Re-serialising would delete what the
user wrote, so the edit is textual and proven by reading it back: every
comment, every trailing comma, every other byte stays where it was.
"""
import json

import pytest

from compartment import cli, clients

VAULT = "/v/memory.vault"

#: Zed's own first-run settings.json, with a server and a comment the user
#: added since.
ZED_FIRST_RUN = """\
// Zed settings
//
// For information on how to configure Zed, see the Zed
// documentation: https://zed.dev/docs/configuring-zed
//
// To see all of Zed's default settings without changing your
// custom settings, run `zed: open default settings` from the
// command palette (cmd-shift-p / ctrl-shift-p)
{
  "ui_font_size": 16,
  "buffer_font_size": 15,
  "theme": {
    "mode": "system",
    "light": "One Light",
    "dark": "One Dark" // the one I use
  },
  /* servers */
  "context_servers": {
    "github": { "command": "gh-mcp", "args": [] }, // keep this one
  },
}
"""

COMMENTS = ["// Zed settings", "// the one I use", "/* servers */",
            "// keep this one", "(cmd-shift-p / ctrl-shift-p)"]


def _lenient(text):
    return clients._plain(clients._jsonc_parse(text))


def test_a_zed_settings_file_with_comments_is_written_keeping_them(tmp_path):
    """The strict reader refused the file Zed itself writes, so `integrate
    zed` printed a block on every machine instead of connecting Zed."""
    p = tmp_path / "settings.json"
    p.write_text(ZED_FIRST_RUN, encoding="utf-8")
    zed = clients.CLIENTS["zed"]

    res = clients.register(zed, VAULT, path=p)

    assert res["written"] is True, res["reason"]
    text = p.read_text(encoding="utf-8")
    for comment in COMMENTS:
        assert comment in text
    data = _lenient(text)
    assert data["context_servers"]["github"] == {"command": "gh-mcp",
                                                 "args": []}
    assert data["context_servers"]["compartment"]["args"][:2] == [
        "--vault", VAULT]
    assert data["theme"]["dark"] == "One Dark"
    assert open(res["backup"], encoding="utf-8").read() == ZED_FIRST_RUN
    assert clients.is_registered(zed, path=p) is True

    # Again: refreshed in place, not duplicated, comments still there.
    clients.register(zed, "/other.vault", path=p)
    again = p.read_text(encoding="utf-8")
    assert again.count('"compartment"') == 1
    assert _lenient(again)["context_servers"]["compartment"]["args"][1] == (
        "/other.vault")
    for comment in COMMENTS:
        assert comment in again

    # And out again: only ours goes.
    assert clients.unregister(zed, path=p) is True
    out = p.read_text(encoding="utf-8")
    assert _lenient(out) == _lenient(ZED_FIRST_RUN)
    for comment in COMMENTS:
        assert comment in out
    assert clients.is_registered(zed, path=p) is False


@pytest.mark.parametrize("text", [
    "// mine\n{}\n",
    "{\n  // nothing here yet\n}\n",
    '{\n\t"theme": "dark", // tabs\n}\n',
    '{"theme": "dark", /* one line */ "vim_mode": true}',
    '{\n  "context_servers": {}, // none yet\n  "vim_mode": true\n}\n',
    '{\n  "context_servers": {\n    // none yet\n  }\n}\n',
    '{\n  "a": [1, 2,], // trailing in an array\n  "b": "//not a comment"\n}',
])
def test_every_shape_of_jsonc_reads_back_with_our_entry_and_out_again(
        text, tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(text, encoding="utf-8")
    zed = clients.CLIENTS["zed"]
    before = _lenient(text)

    assert clients.register(zed, VAULT, path=p)["written"] is True
    after = _lenient(p.read_text(encoding="utf-8"))
    assert after["context_servers"]["compartment"]["args"][1] == VAULT
    del after["context_servers"]["compartment"]
    if "context_servers" not in before:
        assert after.pop("context_servers") == {}
    assert after == before

    assert clients.unregister(zed, path=p) is True
    restored = _lenient(p.read_text(encoding="utf-8"))
    if "context_servers" not in before:
        assert restored.pop("context_servers") == {}
    assert restored == before


@pytest.mark.parametrize("text, why", [
    ('// c\n{"theme": "a", "theme": "b"}', "duplicate key"),
    ("// c\n[1, 2]", "not a JSON object"),
    ('// c\n{"context_servers": []}', "not an object"),
    ("{'single': 'quotes'}", "expected a quoted key"),
    ('{"a": 1 /* never closed', "unterminated comment"),
])
def test_jsonc_this_cannot_edit_safely_is_left_alone(text, why, tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(text, encoding="utf-8")
    res = clients.register(clients.CLIENTS["zed"], VAULT, path=p)
    assert res["written"] is False
    assert why in res["reason"] and "refusing to touch it" in res["reason"]
    assert "compartment" in res["snippet"]
    assert p.read_text(encoding="utf-8") == text


def test_a_vscode_mcp_json_with_comments_is_written(tmp_path):
    p = tmp_path / "mcp.json"
    text = ('{\n\t// my servers\n\t"servers": {\n\t\t"x": {"type": "stdio", '
            '"command": "x"},\n\t},\n\t"inputs": [],\n}\n')
    p.write_text(text, encoding="utf-8")
    res = clients.register(clients.CLIENTS["vscode"], VAULT, path=p)
    assert res["written"] is True, res["reason"]
    out = p.read_text(encoding="utf-8")
    assert "// my servers" in out
    entry = _lenient(out)["servers"]["compartment"]
    assert entry["type"] == "stdio" and entry["args"][1] == VAULT


def test_a_strict_json_client_still_refuses_comments(tmp_path):
    """Cursor reads strict JSON: a file with comments in it is not one
    Cursor can read either, so it is not ours to rewrite."""
    p = tmp_path / "mcp.json"
    text = '{"mcpServers": {}  // a comment\n}'
    p.write_text(text, encoding="utf-8")
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)
    assert res["written"] is False
    assert p.read_text(encoding="utf-8") == text


def test_removing_a_zed_entry_that_cannot_be_taken_out_says_so(
        tmp_path, monkeypatch, capsys):
    """A registration this cannot take out cleanly is reported, with the
    entry to remove, rather than claimed never to have been there."""
    zed = clients.CLIENTS["zed"]
    p = tmp_path / "settings.json"
    p.write_text('// mine\n{"context_servers": {"compartment": '
                 '{"command": "x"}}}\n', encoding="utf-8")
    assert clients.is_registered(zed, path=p) is True
    monkeypatch.setattr(clients, "_jsonc_delete",
                        lambda *a: (_ for _ in ()).throw(ValueError("no")))
    monkeypatch.setattr(zed.__class__, "config_path", lambda self: p)
    cli._integrate_client(zed, type("A", (), {"remove": True,
                                              "vault": VAULT})())
    out = capsys.readouterr().out
    assert "left as it is" in out and "was not in" not in out
    assert json.loads(p.read_text(encoding="utf-8").split("\n", 1)[1])
