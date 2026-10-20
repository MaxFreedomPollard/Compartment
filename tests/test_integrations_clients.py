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


# ------------------------------------------ homes moved by an environment

@pytest.mark.parametrize("key, var, expect", [
    ("codex", "CODEX_HOME", "{d}/config.toml"),
    ("gemini", "GEMINI_CLI_HOME", "{d}/.gemini/settings.json"),
    ("qwen", "QWEN_HOME", "{d}/settings.json"),
    ("copilot-cli", "COPILOT_HOME", "{d}/mcp-config.json"),
    ("opencode", "OPENCODE_CONFIG_DIR", "{d}/opencode.json"),
])
def test_a_home_moved_by_its_variable_is_followed(key, var, expect, home,
                                                  monkeypatch, tmp_path):
    """Each of these moves its whole configuration directory with one
    variable; the default was written whatever it said, a file the client
    never read, and `--list` called a client with only the moved directory
    not installed."""
    d = tmp_path / "moved"
    d.mkdir()
    monkeypatch.setenv(var, str(d))
    c = clients.CLIENTS[key]
    assert str(c.config_path()) == expect.format(d=d).replace("/", os.sep)
    if key == "gemini":
        (d / ".gemini").mkdir()
    assert c.present() is True
    assert clients.register(c, VAULT)["written"] is True
    assert clients.is_registered(c) is True


def test_an_opencode_jsonc_is_written_rather_than_split_in_two(home):
    d = home / ".config" / "opencode"
    d.mkdir(parents=True)
    (d / "opencode.jsonc").write_text('{\n  // theme\n  "theme": "x"\n}\n',
                                      encoding="utf-8")
    oc = clients.CLIENTS["opencode"]
    assert oc.config_path() == d / "opencode.jsonc"
    assert clients.register(oc, VAULT)["written"] is True
    assert not (d / "opencode.json").exists()
    assert "// theme" in (d / "opencode.jsonc").read_text(encoding="utf-8")


def test_gemini_settings_with_comments_are_written(home):
    """Gemini CLI and Qwen Code strip comments before parsing settings."""
    p = home / ".gemini" / "settings.json"
    p.parent.mkdir()
    p.write_text('{\n  // dark mode\n  "theme": "Dracula"\n}\n',
                 encoding="utf-8")
    assert clients.register(clients.CLIENTS["gemini"], VAULT)["written"]
    assert "// dark mode" in p.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS rule")
def test_zed_on_macos_ignores_xdg_config_home(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert clients.CLIENTS["zed"].config_path() == (
        home / ".config" / "zed" / "settings.json")


def test_lm_studio_is_found_where_its_home_pointer_says(home, tmp_path):
    """LM Studio records its home in ~/.lmstudio-home-pointer, which is how
    a home moved in its settings (to another disk, say) is found."""
    moved = tmp_path / "Volumes" / "Big" / "lmstudio"
    moved.mkdir(parents=True)
    (home / ".lmstudio-home-pointer").write_text(f"{moved}\n",
                                                 encoding="utf-8")
    lm = clients.CLIENTS["lmstudio"]
    assert lm.config_path() == moved / "mcp.json"
    assert lm.present() is True


def test_lm_studio_without_a_pointer_uses_its_default_home(home):
    assert clients.CLIENTS["lmstudio"].config_path() == (
        home / ".lmstudio" / "mcp.json")
    (home / ".cache" / "lm-studio").mkdir(parents=True)
    assert clients.CLIENTS["lmstudio"].config_path() == (
        home / ".cache" / "lm-studio" / "mcp.json")


# --------------------------------------------------------------- Continue

def test_continue_is_wired_with_a_file_of_its_own(home):
    """Continue was print-only, since its config.yaml is YAML with comments.
    It also loads every JSON file in ~/.continue/mcpServers in the Claude
    Desktop shape, so it gets a file of its own and nothing of the user's
    is edited."""
    cont = home / ".continue"
    cont.mkdir()
    (cont / "config.yaml").write_text("# mine\nname: x\n", encoding="utf-8")
    c = clients.CLIENTS["continue"]
    assert c.writes is True and c.present() is True

    res = clients.register(c, VAULT)
    assert res["written"] is True
    p = cont / "mcpServers" / "compartment.json"
    assert json.loads(p.read_text(encoding="utf-8"))["mcpServers"][
        "compartment"]["args"][:2] == ["--vault", VAULT]
    assert (cont / "config.yaml").read_text(encoding="utf-8") == (
        "# mine\nname: x\n")
    assert clients.unregister(c) is True


def test_continue_follows_its_global_dir_variable(home, monkeypatch,
                                                  tmp_path):
    monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(tmp_path / "cont"))
    assert clients.CLIENTS["continue"].config_path() == (
        tmp_path / "cont" / "mcpServers" / "compartment.json")
    assert clients.CLIENTS["continue"].present() is False


# ------------------------------------------------------------------ Goose

def test_the_goose_block_is_an_extension_goose_reads(home):
    """Goose was handed `{"mcpServers": ...}` JSON, a shape Goose's
    config.yaml does not have: its servers are extensions, with `cmd`, not
    `command`, and a `type`."""
    goose = clients.CLIENTS["goose"]
    block = clients.snippet(goose, "/Users/ana/My Vault/mémoire.vault")
    lines = block.splitlines()
    assert lines[:3] == ["extensions:", "  compartment:", "    enabled: true"]
    assert "    type: stdio" in lines and "    name: compartment" in lines
    cmd = next(x for x in lines if x.startswith("    cmd: "))
    args = next(x for x in lines if x.startswith("    args: "))
    # Double-quoted YAML scalars are JSON strings.
    assert json.loads(cmd.split(": ", 1)[1]) == clients.executable()
    assert json.loads(args.split(": ", 1)[1])[:2] == [
        "--vault", "/Users/ana/My Vault/mémoire.vault"]
    assert "mcpServers" not in block


class _WindowsOs:
    """`os` as clients.py sees it on Windows, without telling pathlib."""
    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


def test_goose_on_windows_is_under_appdata_block(home, monkeypatch):
    """The XDG path was printed on Windows too, a file Goose never reads:
    there its config is %APPDATA%\\Block\\goose\\config\\config.yaml."""
    monkeypatch.setattr(clients, "os", _WindowsOs())
    p = clients.CLIENTS["goose"].config_path()
    assert p == (home / "AppData" / "Roaming" / "Block" / "goose" / "config"
                 / "config.yaml")


def test_goose_path_root_moves_its_config(home, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "g"))
    assert clients.CLIENTS["goose"].config_path() == (
        tmp_path / "g" / "config" / "config.yaml")
    monkeypatch.setenv("GOOSE_PATH_ROOT", "relative")      # ignored by Goose
    assert clients.CLIENTS["goose"].config_path() == (
        home / ".config" / "goose" / "config.yaml")


# ---------------------------------------------------------- newer clients

@pytest.mark.parametrize("key, rel, root, typed", [
    ("kiro", ".kiro/settings/mcp.json", "mcpServers", False),
    ("amazon-q", ".aws/amazonq/mcp.json", "mcpServers", False),
    ("junie", ".junie/mcp/mcp.json", "mcpServers", False),
    ("factory", ".factory/mcp.json", "mcpServers", False),
    ("crush", ".config/crush/crush.json", "mcp", True),
])
def test_newer_clients_are_known_and_wired(key, rel, root, typed, home):
    c = clients.CLIENTS[key]
    p = home.joinpath(*rel.split("/"))
    assert c.config_path() == p
    assert c.present() is False
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({root: {"theirs": {"command": "x"}}}),
                 encoding="utf-8")
    assert c.present() is True
    assert clients.register(c, VAULT)["written"] is True
    servers = json.loads(p.read_text(encoding="utf-8"))[root]
    assert servers["theirs"] == {"command": "x"}
    assert servers["compartment"]["args"][:2] == ["--vault", VAULT]
    assert ("type" in servers["compartment"]) is typed


def test_kiro_and_crush_follow_their_variables(home, monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "k"))
    monkeypatch.setenv("CRUSH_GLOBAL_CONFIG", str(tmp_path / "c"))
    assert clients.CLIENTS["kiro"].config_path() == (
        tmp_path / "k" / "settings" / "mcp.json")
    assert clients.CLIENTS["crush"].config_path() == (
        tmp_path / "c" / "crush.json")


def test_junie_is_present_before_its_first_server(home):
    (home / ".junie").mkdir()
    assert clients.CLIENTS["junie"].present() is True
    assert clients.resolve("droid").key == "factory"
    assert clients.resolve("amazonq").key == "amazon-q"


def test_flathub_vs_code_on_linux_is_found_and_written(home, monkeypatch):
    """The Flathub VS Code keeps its User directory in its sandbox,
    ~/.var/app/com.visualstudio.code/config/Code/User; only the native
    path was looked at, so it was not found and never written."""
    monkeypatch.setattr(clients.sys, "platform", "linux")
    if os.name == "nt":
        pytest.skip("POSIX layout")
    user = (home / ".var" / "app" / "com.visualstudio.code" / "config"
            / "Code" / "User")
    user.mkdir(parents=True)
    vscode = clients.CLIENTS["vscode"]
    assert vscode.config_path() == user / "mcp.json"
    assert vscode.present() is True
    # A native install, where there is one, is still the one written.
    (home / ".config" / "Code" / "User").mkdir(parents=True)
    assert vscode.config_path() == (home / ".config" / "Code" / "User"
                                    / "mcp.json")
