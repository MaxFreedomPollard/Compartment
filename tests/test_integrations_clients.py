"""Where each client reads its MCP servers today.

Every case puts a fake home directory under tmp_path and lays out only what
the client itself would have created, then checks that `integrate` finds
the client and writes the file that client reads."""
import json
import os
import sys

import pytest

from compartment import clients

VAULT = "/v/memory.vault"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(clients.Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("APPDATA", str(h / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    for var in ("CLINE_DIR", "CLINE_DATA_DIR", "CLINE_MCP_SETTINGS_PATH",
                "CODEX_HOME", "GEMINI_CLI_HOME", "QWEN_HOME", "COPILOT_HOME",
                "OPENCODE_CONFIG_DIR", "CRUSH_GLOBAL_CONFIG", "KIRO_HOME",
                "GOOSE_PATH_ROOT", "CONTINUE_GLOBAL_DIR"):
        monkeypatch.delenv(var, raising=False)
    return h


def _editor_storage(editor, ext):
    """An extension's globalStorage directory in `editor`, as the editor
    creates it the first time the extension runs."""
    d = clients._vscode_user(editor) / "globalStorage" / ext
    d.mkdir(parents=True)
    return d


def _servers(p, root="mcpServers"):
    return clients._plain(clients._jsonc_parse(
        p.read_text(encoding="utf-8")))[root]


# ------------------------------------------------------------------ Cline

def test_cline_4_is_written_in_its_shared_settings_file(home):
    """Cline 4 reads ~/.cline/data/settings/cline_mcp_settings.json and
    imports the old globalStorage file once, so a server written there
    afterwards was never seen: `integrate cline` said it was wired."""
    (home / ".cline").mkdir()
    _editor_storage("Code", "saoudrizwan.claude-dev")
    cline = clients.CLIENTS["cline"]

    assert cline.present() is True
    want = home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
    assert cline.config_path() == want
    assert clients.register(cline, VAULT)["written"] is True
    assert "compartment" in _servers(want)


def test_cline_honours_its_directory_variables(home, monkeypatch, tmp_path):
    cline = clients.CLIENTS["cline"]
    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "c"))
    assert cline.config_path() == (tmp_path / "c" / "data" / "settings"
                                   / "cline_mcp_settings.json")
    monkeypatch.setenv("CLINE_DATA_DIR", str(tmp_path / "d"))
    assert cline.config_path() == (tmp_path / "d" / "settings"
                                   / "cline_mcp_settings.json")
    monkeypatch.setenv("CLINE_MCP_SETTINGS_PATH", str(tmp_path / "m.json"))
    assert cline.config_path() == tmp_path / "m.json"


def test_an_older_cline_in_cursor_is_found_and_written_there(home):
    """Only VS Code's own storage was looked at, so Cline installed in
    Cursor (or Windsurf, VSCodium, Insiders) was reported as not installed
    and wiring it wrote into a VS Code that was not there."""
    storage = _editor_storage("Cursor", "saoudrizwan.claude-dev")
    cline = clients.CLIENTS["cline"]
    assert cline.present() is True
    assert cline.config_path() == (storage / "settings"
                                   / "cline_mcp_settings.json")


def test_roo_code_in_windsurf_is_found(home):
    storage = _editor_storage("Windsurf", "rooveterinaryinc.roo-cline")
    roo = clients.CLIENTS["roo"]
    assert roo.present() is True
    assert roo.config_path() == storage / "settings" / "mcp_settings.json"


@pytest.mark.skipif(sys.platform == "darwin" or os.name == "nt",
                    reason="Flatpak is Linux")
def test_an_extension_in_flatpak_vs_code_is_found(home):
    storage = (home / ".var" / "app" / "com.visualstudio.code" / "config"
               / "Code" / "User" / "globalStorage"
               / "rooveterinaryinc.roo-cline")
    storage.mkdir(parents=True)
    assert clients.CLIENTS["roo"].config_path().parent.parent == storage


# ------------------------------------------------------------------- Kilo

def test_kilo_code_7_gets_an_opencode_style_entry_in_its_global_config(home):
    """Kilo Code 7 reads MCP servers from ~/.config/kilo/kilo.jsonc under
    "mcp"; the old mcp_settings.json in VS Code's storage is read only for
    migration, and never by the Kilo CLI."""
    kilo_dir = home / ".config" / "kilo"
    kilo_dir.mkdir(parents=True)
    kilo = clients.CLIENTS["kilo"]
    assert kilo.present() is True
    assert kilo.config_path() == kilo_dir / "kilo.jsonc"

    clients.register(kilo, VAULT)
    entry = _servers(kilo_dir / "kilo.jsonc", "mcp")["compartment"]
    assert entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][1:3] == ["--vault", VAULT]


def test_kilo_code_writes_the_config_file_it_already_has(home):
    kilo_dir = home / ".config" / "kilo"
    kilo_dir.mkdir(parents=True)
    (kilo_dir / "opencode.json").write_text(
        '{\n  // mine\n  "model": "x",\n}\n', encoding="utf-8")
    kilo = clients.CLIENTS["kilo"]
    assert kilo.config_path() == kilo_dir / "opencode.json"
    assert clients.register(kilo, VAULT)["written"] is True
    text = (kilo_dir / "opencode.json").read_text(encoding="utf-8")
    assert "// mine" in text and '"compartment"' in text


def test_kilo_code_installed_in_an_editor_but_never_run_is_present(home):
    _editor_storage("VSCodium", "kilocode.kilo-code")
    assert clients.CLIENTS["kilo"].present() is True
