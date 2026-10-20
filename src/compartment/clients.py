"""Every other program on this machine that speaks MCP, and where each one
keeps its server list.

Compartment is a stdio MCP server, which means it already works with every
one of these. Nothing had to be built for Cursor or Zed or LM Studio to be
able to talk to it; they could all do that the day the server existed. What
was missing was the two minutes of finding the right file, learning that this
one spells the key `servers` and that one spells it `context_servers`, and
editing JSON by hand without breaking the servers already in there.

So this module is not an integration layer. It is a table of where those
files live and what shape they want, plus one writer careful enough to be
pointed at somebody's real configuration.

Careful means four things, and they are the whole reason this is not a
one-liner:

  - Merge, never replace. These files hold the user's other MCP servers, and
    a writer that drops them is worse than no writer at all.
  - Only write where the client is actually installed. The parent directory
    existing is the proof. Otherwise `--all` scatters configuration for
    programs nobody has.
  - Back up first, byte-for-byte, before touching anything.
  - Refuse anything we cannot parse. A config with comments in it, or one
    somebody hand-edited into invalid JSON, is not ours to guess at. We print
    the block for them to paste instead, which is what `integrate openclaw`
    has always done on failure.

Clients whose configuration is not a file we can safely write - the ones that
keep it in an application database, or in Redux state, or in a YAML document
whose comments we would destroy - are listed too, with `writes = False`. They
are not second-class: the user gets the exact block and the exact place to
put it, which is all the auto-writing ones do anyway, minus the typing.
"""
from __future__ import annotations

import codecs
import errno
import json
import os
import re
import secrets
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

NAME = "compartment"

#: Suffix for the copy taken before any file is modified.
BACKUP_SUFFIX = ".compartment-backup"


# --- where the per-platform roots are --------------------------------------

def _appdata() -> Path:
    """%APPDATA% on Windows, with the documented default if it is unset."""
    return Path(os.environ.get("APPDATA")
                or Path.home() / "AppData" / "Roaming")


def _xdg() -> Path:
    """$XDG_CONFIG_HOME, or the ~/.config it defaults to."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _app_support(name: str) -> Path:
    """A macOS Application Support directory, or the equivalent elsewhere.

    Electron apps use all three of these for the same directory, which is why
    so many of the clients below differ only in the name passed here.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name
    if os.name == "nt":
        return _appdata() / name
    return _xdg() / name


def _vscode_user(app: str = "Code") -> Path:
    """VS Code's User directory, which several extensions nest inside.

    Roo Code, and older Cline and Kilo Code, are VS Code extensions whose
    settings live under the editor's storage rather than anywhere of their
    own.
    """
    return _app_support(app) / "User"


#: The editors an extension may be installed in, by the name of their user
#: data folder: VS Code, its Insiders build, VSCodium, and the forks that
#: run VS Code extensions (vscode-docs, VSCodium's prepare_vscode.sh).
_EDITORS = ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf")


def _editor_users() -> list[Path]:
    """Every editor User directory an extension could keep storage in,
    the Flathub builds of VS Code and VSCodium included (Flatpak sets their
    XDG_CONFIG_HOME to ~/.var/app/<id>/config)."""
    users = [_vscode_user(app) for app in _EDITORS]
    if sys.platform != "darwin" and os.name != "nt":
        flat = Path.home() / ".var" / "app"
        users += [flat / "com.visualstudio.code" / "config" / "Code" / "User",
                  flat / "com.vscodium.codium" / "config" / "VSCodium"
                  / "User"]
    return users


def _ext_storage(ext: str) -> Path | None:
    """The globalStorage directory of extension `ext` in the first editor
    that has one, or None when no editor has run it."""
    for user in _editor_users():
        d = user / "globalStorage" / ext
        try:
            if d.is_dir():
                return d
        except OSError:
            pass
    return None


def _ext_settings(ext: str, filename: str) -> Callable[[], Path]:
    """A VS Code extension's own settings file, by extension id, in the
    editor it is installed in.

    Only VS Code's own storage used to be looked at, so the extension in
    Cursor, Windsurf, VSCodium or VS Code Insiders was reported as not
    installed, and wiring it wrote a file in a VS Code that might not even
    be there. VS Code is still the answer when no editor has run it."""
    def path() -> Path:
        storage = _ext_storage(ext) or (_vscode_user() / "globalStorage"
                                        / ext)
        return storage / "settings" / filename
    return path


def _env_dir(var: str) -> Path | None:
    """The directory an environment variable names, or None when it is
    unset or blank."""
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser() if raw else None


def _cline_dir() -> Path:
    return _env_dir("CLINE_DIR") or Path.home() / ".cline"


def _cline_settings() -> Path:
    """cline_mcp_settings.json, where the Cline that is installed reads it.

    Cline 4 keeps one file for the VS Code extension, the CLI and the
    JetBrains plugin, ~/.cline/data/settings/cline_mcp_settings.json
    (CLINE_MCP_SETTINGS_PATH, CLINE_DATA_DIR and CLINE_DIR move it;
    sdk/packages/shared/src/storage/paths.ts), and imports the old
    globalStorage file once, the first time it runs. A server written to
    the old file after that is never read. An older Cline, one that has
    not created ~/.cline, still reads the old file in its editor."""
    explicit = _env_dir("CLINE_MCP_SETTINGS_PATH")
    if explicit:
        return explicit
    data = _env_dir("CLINE_DATA_DIR")
    current = ((data or _cline_dir() / "data") / "settings"
               / "cline_mcp_settings.json")
    try:
        if data or current.exists() or _cline_dir().is_dir():
            return current
    except OSError:
        return current
    legacy = _ext_storage("saoudrizwan.claude-dev")
    if legacy is not None:
        return legacy / "settings" / "cline_mcp_settings.json"
    return current


def _kilo_config() -> Path:
    """Kilo Code's global config.

    Kilo Code 7 is built on OpenCode and reads MCP servers from
    ~/.config/kilo (XDG_CONFIG_HOME, on every platform): the first of
    kilo.jsonc, kilo.json, opencode.jsonc, opencode.json and config.json
    that exists, kilo.jsonc when none does (packages/opencode/src/config/
    config.ts). The extension's old mcp_settings.json is read only from VS
    Code's own storage, only for migration, and never by the Kilo CLI."""
    d = _xdg() / "kilo"
    for name in ("kilo.jsonc", "kilo.json", "opencode.jsonc",
                 "opencode.json", "config.json"):
        if (d / name).exists():
            return d / name
    return d / "kilo.jsonc"


def _home(*parts: str) -> Callable[[], Path]:
    """A path under the user's home directory, the same on every platform."""
    return lambda: Path.home().joinpath(*parts)


def _home_from(var: str, *default: str) -> Path:
    """The directory `var` names, or ~/<default...> when it is unset."""
    return _env_dir(var) or Path.home().joinpath(*default)


def _zed_settings() -> Path:
    """Zed's settings.json (crates/paths/src/paths.rs): %APPDATA%\\Zed on
    Windows, the XDG config directory on Linux, and ~/.config/zed on macOS
    whatever XDG_CONFIG_HOME says, since Zed does not consult it there.
    Taking XDG_CONFIG_HOME on a Mac that sets it wrote a file Zed never
    read."""
    if os.name == "nt":
        return _appdata() / "Zed" / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / ".config" / "zed" / "settings.json"
    return _xdg() / "zed" / "settings.json"


def _opencode_config() -> Path:
    """OpenCode's global config: OPENCODE_CONFIG_DIR, or opencode under the
    XDG config directory on every platform, and in it opencode.json or
    opencode.jsonc, both of which it loads. The one that exists is
    written; a new opencode.json beside an opencode.jsonc would split the
    user's config in two."""
    d = _env_dir("OPENCODE_CONFIG_DIR") or _xdg() / "opencode"
    if not (d / "opencode.json").exists() and (d / "opencode.jsonc").exists():
        return d / "opencode.jsonc"
    return d / "opencode.json"


def _lmstudio_home() -> Path:
    """LM Studio's home, as LM Studio finds it (lmstudio-js,
    findLMStudioHome.ts): the path in ~/.lmstudio-home-pointer, which LM
    Studio writes and which is how a home moved in its settings is found;
    else ~/.cache/lm-studio, where older versions lived; else ~/.lmstudio."""
    h = Path.home()
    try:
        pointer = (h / ".lmstudio-home-pointer").read_text(
            encoding="utf-8").strip()
        if pointer:
            return Path(pointer).expanduser()
    except (OSError, UnicodeDecodeError):
        pass
    legacy = h / ".cache" / "lm-studio"
    return legacy if legacy.is_dir() else h / ".lmstudio"


def _goose_config() -> Path:
    """Goose's config.yaml (crates/goose/src/config/paths.rs):
    $GOOSE_PATH_ROOT/config when that is an absolute path; otherwise the
    XDG config directory on macOS and Linux, and on Windows
    %APPDATA%\\Block\\goose\\config, where the XDG path that was printed
    named a file Goose never reads."""
    root = _env_dir("GOOSE_PATH_ROOT")
    if root is not None and root.is_absolute():
        return root / "config" / "config.yaml"
    if os.name == "nt":
        return _appdata() / "Block" / "goose" / "config" / "config.yaml"
    return _xdg() / "goose" / "config.yaml"


def _yaml_str(v: str) -> str:
    """A YAML double-quoted scalar. YAML 1.2 is a superset of JSON, so a
    JSON string is one, escapes and all."""
    return json.dumps(v, ensure_ascii=False)


def _goose_block(entry: dict) -> str:
    """The extension as Goose's config.yaml spells it: under `extensions:`,
    with `cmd` rather than `command` and a `type` (ExtensionConfig::Stdio in
    crates/goose/src/agents/extension.rs). The `mcpServers` JSON that used
    to be printed for Goose is not a shape Goose reads at all."""
    return "\n".join([
        "extensions:",
        f"  {NAME}:",
        "    enabled: true",
        "    type: stdio",
        f"    name: {NAME}",
        "    description: Encrypted offline memory",
        f"    cmd: {_yaml_str(entry['command'])}",
        "    args: [" + ", ".join(_yaml_str(a) for a in entry["args"]) + "]",
        "    timeout: 300",
    ])


def _librechat_block(entry: dict) -> str:
    """The server under librechat.yaml's `mcpServers:` mapping, as YAML."""
    return "\n".join([
        "mcpServers:",
        f"  {NAME}:",
        "    type: stdio",
        f"    command: {_yaml_str(entry['command'])}",
        "    args: [" + ", ".join(_yaml_str(a) for a in entry["args"]) + "]",
    ])


# --- the shape each client wants an entry in -------------------------------

def _entry_plain(command: str, args: list[str]) -> dict:
    """`{"command": ..., "args": [...]}`, which is what most of them take."""
    return {"command": command, "args": list(args)}


def _entry_vscode(command: str, args: list[str]) -> dict:
    """VS Code names the transport explicitly on every server."""
    return {"type": "stdio", "command": command, "args": list(args)}


def _entry_opencode(command: str, args: list[str]) -> dict:
    """OpenCode takes one array rather than a command and its arguments, and
    wants to be told the server is local and switched on."""
    return {"type": "local", "command": [command, *args], "enabled": True}


@dataclass(frozen=True)
class Client:
    """One program that reads MCP servers from a file we know the shape of."""

    key: str                       #: what the user types after `integrate`
    display: str                   #: what we call it when we talk about it
    path: Callable[[], Path]       #: its config file on this platform
    root: str = "mcpServers"       #: the key its servers live under
    entry: Callable[[str, list[str]], dict] = _entry_plain
    kind: str = "json"             #: "json", "toml", or "manual"
    writes: bool = True            #: whether we are willing to edit it
    note: str = ""                 #: anything the user needs to know after
    aliases: tuple[str, ...] = field(default=())
    #: Whether the client reads its file as JSON with comments, so a file
    #: holding comments or trailing commas is edited as text, not refused.
    jsonc: bool = False
    #: Whether the client is installed, when the config file's directory is
    #: not the proof (it may not exist until the first server is added).
    installed: Callable[[], bool] | None = None
    #: The block to print, from the entry, for a client whose file is not
    #: JSON (Goose's YAML); JSON under `root` when None.
    block: Callable[[dict], str] | None = None

    def config_path(self) -> Path:
        return self.path()

    def present(self) -> bool:
        """Whether this client looks installed.

        The config file itself often does not exist until the user adds their
        first server, so the directory it would live in is the real signal.
        A bare filename (LibreChat, Open WebUI) has no directory of its own:
        its parent is wherever the command runs, so only the file counts.
        """
        p = self.config_path()
        if not p.is_absolute():
            return p.exists()
        if p.exists() or p.parent.is_dir():
            return True
        if self.installed is not None:
            try:
                return bool(self.installed())
            except OSError:
                return False
        return False


# --- the table -------------------------------------------------------------
#
# Every path here was taken from the client's own documentation or from a
# maintained implementation, not from a blog post. Anything that could not be
# confirmed is `writes=False` rather than a guess, because the cost of being
# wrong is somebody else's configuration.

CLIENTS: dict[str, Client] = {c.key: c for c in [

    # -- coding agents, plain `mcpServers` ---------------------------------
    Client("cursor", "Cursor", _home(".cursor", "mcp.json")),
    Client("windsurf", "Windsurf",
           _home(".codeium", "windsurf", "mcp_config.json")),
    Client("cline", "Cline", _cline_settings,
           installed=lambda: (_cline_dir().is_dir() or _ext_storage(
               "saoudrizwan.claude-dev") is not None),
           note="reload the editor window, or restart the Cline CLI, to "
                "pick it up"),
    # Roo Code's repository was archived in May 2026; the extension still
    # reads globalStorage/rooveterinaryinc.roo-cline/settings.
    Client("roo", "Roo Code",
           _ext_settings("rooveterinaryinc.roo-cline", "mcp_settings.json"),
           aliases=("roo-code",),
           installed=lambda: _ext_storage(
               "rooveterinaryinc.roo-cline") is not None,
           note="reload the editor window to pick it up"),
    Client("kilo", "Kilo Code", _kilo_config, root="mcp",
           entry=_entry_opencode, jsonc=True, aliases=("kilo-code",),
           installed=lambda: ((_xdg() / "kilo").is_dir() or _ext_storage(
               "kilocode.kilo-code") is not None),
           note="reload the editor window, or restart the Kilo CLI, to "
                "pick it up"),
    # GEMINI_CLI_HOME stands in for the home directory, .gemini under it;
    # QWEN_HOME and COPILOT_HOME are the directory itself. Both CLIs strip
    # comments before parsing settings.json.
    Client("gemini", "Gemini CLI",
           lambda: (_env_dir("GEMINI_CLI_HOME") or Path.home()) / ".gemini"
           / "settings.json", jsonc=True, aliases=("gemini-cli",)),
    Client("qwen", "Qwen Code CLI",
           lambda: _home_from("QWEN_HOME", ".qwen") / "settings.json",
           jsonc=True, aliases=("qwen-code",)),
    Client("copilot-cli", "GitHub Copilot CLI",
           lambda: _home_from("COPILOT_HOME", ".copilot") / "mcp-config.json"),
    # Kiro (IDE and CLI) and the Amazon Q Developer CLI it replaced; Kiro's
    # directory moves with KIRO_HOME (kirodotdev/KiroCrew, config/paths.py),
    # Amazon Q's is fixed (crates/chat-cli/src/util/paths.rs).
    Client("kiro", "Kiro", lambda: _home_from("KIRO_HOME", ".kiro")
           / "settings" / "mcp.json", aliases=("kiro-cli",)),
    Client("amazon-q", "Amazon Q Developer CLI",
           _home(".aws", "amazonq", "mcp.json"), aliases=("amazonq",)),
    # Junie's user-level file, from JetBrains' own skill for driving it
    # (JetBrains/junie, hermes-plugin/skills/junie/SKILL.md).
    Client("junie", "Junie", _home(".junie", "mcp", "mcp.json"),
           installed=lambda: (Path.home() / ".junie").is_dir()),
    # Factory's droid; stdio is the default type (docs/cli/configuration/
    # mcp.mdx in Factory-AI/factory).
    Client("factory", "Factory Droid", _home(".factory", "mcp.json"),
           aliases=("droid",)),
    # Oh My Pi reads a project-level .omp/mcp.json as well, but that one is
    # relative to whatever repository you happen to be in, so the user-level
    # file is the only one an integrate command can honestly write.
    Client("omp", "Oh My Pi", _home(".omp", "agent", "mcp.json"),
           aliases=("oh-my-pi",),
           note="run `/mcp reload` in omp to pick it up without restarting"),

    # -- coding agents that spell the key differently ----------------------
    Client("vscode", "VS Code", lambda: _vscode_user() / "mcp.json",
           root="servers", entry=_entry_vscode, jsonc=True),
    Client("vscode-insiders", "VS Code Insiders",
           lambda: _vscode_user("Code - Insiders") / "mcp.json",
           root="servers", entry=_entry_vscode, jsonc=True),
    # Zed writes its settings.json with a block of // comments on first
    # run, so it is edited as JSON with comments: comments kept.
    Client("zed", "Zed", _zed_settings, root="context_servers", jsonc=True),
    # Crush: $CRUSH_GLOBAL_CONFIG/crush.json, else crush/crush.json under
    # XDG_CONFIG_HOME or ~/.config on every platform, servers under "mcp"
    # with a required type (internal/config/load.go, config.go).
    Client("crush", "Crush",
           lambda: (_env_dir("CRUSH_GLOBAL_CONFIG") or _xdg() / "crush")
           / "crush.json", root="mcp", entry=_entry_vscode),
    # The legacy "mcp": {name: entry} shape, which every release reads and
    # which current ones merge with the newer "mcp": {"servers": ...}.
    Client("opencode", "OpenCode", _opencode_config,
           root="mcp", entry=_entry_opencode, jsonc=True),

    # -- Codex keeps TOML --------------------------------------------------
    # CODEX_HOME moves the whole directory (codex-rs/utils/home-dir).
    Client("codex", "Codex CLI",
           lambda: _home_from("CODEX_HOME", ".codex") / "config.toml",
           root="mcp_servers", kind="toml", aliases=("codex-cli",)),

    # -- desktop and self-hosted chat apps ---------------------------------
    Client("lmstudio", "LM Studio", lambda: _lmstudio_home() / "mcp.json",
           aliases=("lm-studio",),
           note="LM Studio reloads mcp.json on save, no restart needed"),
    Client("anythingllm", "AnythingLLM",
           lambda: (_app_support("anythingllm-desktop") / "storage"
                    / "plugins" / "anythingllm_mcp_servers.json"),
           note="open the Agent Skills page in AnythingLLM to start it"),
    Client("boltai", "BoltAI", _home(".boltai", "mcp.json")),
    Client("5ire", "5ire", lambda: _app_support("5ire") / "mcp.json"),
    # Trae is the one that does not follow its own pattern: capitalised with a
    # User/ level on macOS and Windows, lowercase without one on Linux.
    Client("trae", "Trae",
           lambda: (_app_support("Trae") / "User" / "mcp.json"
                    if sys.platform == "darwin" or os.name == "nt"
                    else _xdg() / "trae" / "mcp.json")),

    # Continue loads every *.json under its global mcpServers folder, in the
    # Claude Desktop shape (core/context/mcp/json/loadJsonMcpConfigs.ts), so
    # it gets a file of its own: nothing of the user's is edited, and its
    # config.yaml, whose comments a rewrite would lose, is never touched.
    Client("continue", "Continue",
           lambda: (_home_from("CONTINUE_GLOBAL_DIR", ".continue")
                    / "mcpServers" / "compartment.json"),
           installed=lambda: _home_from("CONTINUE_GLOBAL_DIR",
                                        ".continue").is_dir(),
           jsonc=True,
           note="Continue loads it from its mcpServers folder; reload the "
                "editor window to pick it up"),

    # -- known, documented, but not ours to write --------------------------
    #
    # Each of these keeps its configuration somewhere a merge cannot be made
    # safely: a YAML document whose comments and anchors we would flatten, or
    # an application database, or a settings blob with no stable on-disk
    # shape. Printing the exact block is honest; writing a guess is not.
    Client("goose", "Goose", _goose_config, kind="manual", writes=False,
           block=_goose_block,
           note="add this to config.yaml (merge it into `extensions:` if "
                "you have one), or run `goose configure` and add it as a "
                "command-line extension"),
    Client("librechat", "LibreChat", lambda: Path("librechat.yaml"),
           kind="manual", writes=False, block=_librechat_block,
           note="merge this into `mcpServers:` in your librechat.yaml, then "
                "restart LibreChat"),
    Client("jan", "Jan", lambda: _app_support("Jan") / "data"
           / "mcp_config.json", kind="manual", writes=False,
           note="Settings > MCP Servers, or edit mcp_config.json"),
    Client("cherry-studio", "Cherry Studio",
           lambda: _app_support("CherryStudio"),
           kind="manual", writes=False,
           note="Cherry Studio keeps MCP settings in application state, not "
                "a config file: paste this in Settings > MCP Servers"),
    Client("chatbox", "Chatbox", lambda: _app_support("chatbox"),
           kind="manual", writes=False,
           note="paste this in Settings > MCP > Add Server"),
    Client("msty", "Msty", lambda: _app_support("Msty"),
           kind="manual", writes=False,
           note="paste this in Toolbox > Tools, with STDIO / JSON selected"),
    Client("witsy", "Witsy", lambda: _app_support("Witsy"),
           kind="manual", writes=False,
           note="paste this in Settings > MCP Servers"),
    Client("open-webui", "Open WebUI", lambda: Path("mcpo.json"),
           kind="manual", writes=False,
           note="Open WebUI reads HTTP, not stdio: put this in an mcpo "
                "config and point Open WebUI at mcpo's OpenAPI endpoint"),
]}

#: Every spelling the CLI will accept, including the aliases.
ALIASES: dict[str, str] = {
    **{k: k for k in CLIENTS},
    **{a: c.key for c in CLIENTS.values() for a in c.aliases},
}


def resolve(key: str) -> Client | None:
    """The client a user meant, accepting its aliases and any casing."""
    return CLIENTS.get(ALIASES.get(key.strip().lower(), ""))


def executable() -> str:
    """The compartment command to put in somebody else's config.

    An absolute path is better than the bare name, because a GUI application
    launched from the Dock does not inherit the shell PATH that put
    `compartment` on it. On a Mac where the .app is the whole install there
    is no `compartment` on any PATH to inherit either: the installer puts
    Compartment.app in /Applications and nothing anywhere else, so falling
    back to the bare name wrote a config naming a command that does not
    exist on that machine - registered, reported as connected, and dead on
    the first launch.

    The console script beside the running interpreter is the one that is
    certainly there. Inside Compartment.app that is the copy sealed in the
    bundle, which is exactly the one that should be answering.

    Never anything from a bundle's MacOS folder. The launcher there is named
    `Compartment`, macOS filesystems are case-insensitive by default, and a
    config pointed at it would start the menu bar app every time a client
    tried to open a memory server.

    `pip install --user`, which is what the install scripts for all three
    platforms run, puts the script in the user scheme's directory
    (~/Library/Python/3.X/bin, ~/.local/bin, or on Windows
    %APPDATA%\\Python\\PythonXY\\Scripts), beside no interpreter and on no
    PATH a GUI app or the panel's child process has. Looking only beside the interpreter and then on PATH
    found nothing there and wrote the bare name: every client then failed
    to start the server with "spawn compartment ENOENT". So the scripts
    directory of the scheme this package was actually installed under is
    asked first; it is the one pip wrote the script into, by definition.

    A script found inside a package manager's versioned tree (Homebrew's
    Cellar/compartment/<version>/libexec/bin) is dead after the next
    upgrade and cleanup, while the link the package manager keeps on PATH
    for it (/opt/homebrew/bin/compartment, and pipx's and uv's
    ~/.local/bin links) is repointed by every upgrade. So when such a link
    resolves to the very script found, the link is what is written.
    """
    found = _installed_script()
    if found is None:
        which = shutil.which(NAME)
        return which or NAME
    return str(_stable_link(found))


def _script_in(directory: Path) -> Path | None:
    """The compartment console script in `directory`, if there is one and it
    is not an app bundle's launcher."""
    for c in (directory / NAME, directory / f"{NAME}.exe"):
        try:
            if c.is_file() and c.parent.name != "MacOS":
                return c
        except OSError:
            pass
    return None


def _package_root() -> Path:
    """The site-packages directory this copy of compartment was imported
    from. An editable install's is the source tree, which no scheme
    names."""
    return Path(__file__).resolve().parent.parent


def _scheme_scripts() -> list[Path]:
    """The scripts directory of each install scheme whose library directory
    holds this package: where pip put the console script for this copy.

    The schemes are expanded against the running interpreter's own prefix,
    the venv's inside a venv, and the user scheme is the one pip uses for
    `--user` on this platform (osx_framework_user for python.org's macOS
    builds, nt_user on Windows, posix_user elsewhere)."""
    import sysconfig
    try:
        here = _package_root()
    except OSError:
        return []
    names = []
    for get in (sysconfig.get_default_scheme,
                lambda: sysconfig.get_preferred_scheme("prefix"),
                lambda: sysconfig.get_preferred_scheme("user")):
        try:
            names.append(get())
        except (KeyError, ValueError, AttributeError):
            pass
    out = []
    for scheme in dict.fromkeys(names):
        try:
            paths = sysconfig.get_paths(scheme, vars={
                "base": sys.prefix, "platbase": sys.exec_prefix})
            libs = {Path(paths["purelib"]).resolve(),
                    Path(paths["platlib"]).resolve()}
        except (KeyError, ValueError, OSError):
            continue
        if here in libs:
            out.append(Path(paths["scripts"]))
    return out


def _installed_script() -> Path | None:
    """The console script that belongs to the running copy of compartment,
    or None when there is none to be found (a source checkout run with
    `python -m`)."""
    prefix = Path(sys.prefix)
    for d in (*_scheme_scripts(), prefix / "bin", prefix / "Scripts"):
        hit = _script_in(d)
        if hit is not None:
            return hit
    beside = Path(sys.executable).parent / NAME
    try:
        if beside.is_file() and beside.parent.name != "MacOS":
            return beside
    except OSError:
        pass
    return None


def _link_dirs() -> list[Path]:
    """Where package managers keep the links they repoint on upgrade."""
    h = Path.home()
    dirs = [h / ".local" / "bin", Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"), Path("/home/linuxbrew/.linuxbrew/bin"),
            h / ".linuxbrew" / "bin", h / ".nix-profile" / "bin",
            Path("/run/current-system/sw/bin"), h / "bin"]
    user = os.environ.get("USER")
    if user:
        dirs.append(Path("/etc/profiles/per-user") / user / "bin")
    return dirs


def _stable_link(script: Path) -> Path:
    """A link on PATH, or in a package manager's bin directory, that
    resolves to `script` itself; `script` when there is none."""
    try:
        real = os.path.realpath(script)
    except (OSError, ValueError):
        return script
    which = shutil.which(NAME)
    candidates = ([Path(which)] if which else []) + [d / NAME
                                                     for d in _link_dirs()]
    for c in candidates:
        try:
            if (c.is_absolute() and c != script and c.is_symlink()
                    and os.path.realpath(c) == real):
                return c
        except (OSError, ValueError):
            continue
    return script


def server_args(vault: str, caller: str) -> list[str]:
    """The arguments every client is given, matching `integrate openclaw`."""
    return ["--vault", vault, "--caller", caller, "serve"]


def entry_for(client: Client, vault: str, caller: str | None = None) -> dict:
    """The JSON object this client wants for one server."""
    return client.entry(executable(),
                        server_args(vault, caller or client.key))


def snippet(client: Client, vault: str, caller: str | None = None) -> str:
    """What to print when we are not writing the file ourselves."""
    if client.kind == "toml":
        return _toml_block(entry_for(client, vault, caller))
    if client.block is not None:
        return client.block(entry_for(client, vault, caller))
    return json.dumps({client.root: {NAME: entry_for(client, vault, caller)}},
                      indent=2)


# --- reading and writing ---------------------------------------------------

#: Byte order marks, longest first: UTF-32's little-endian mark begins
#: with UTF-16's.
_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32"),
         (codecs.BOM_UTF32_BE, "utf-32"), (codecs.BOM_UTF16_LE, "utf-16"),
         (codecs.BOM_UTF16_BE, "utf-16"))


def read_config_text(path: Path) -> str:
    """A config file's text, in whichever Unicode encoding it was saved.

    Windows editors do not all save UTF-8 without a mark. Notepad before
    Windows 10 1903 and PowerShell 5's `Out-File -Encoding utf8` put a UTF-8
    byte order mark first; PowerShell 5's `>` and `Out-File` write UTF-16.
    Those are the tools the Claude Desktop and Cursor guides send Windows
    users to, and a file saved by them was refused as "not valid JSON" (the
    mark is not whitespace to json.loads) or failed to decode at all, so
    the user got a block to paste into the very file that had just been
    refused. A marked file is decoded by its mark; anything unmarked must
    be UTF-8, as before. What is written back is always UTF-8 without a
    mark, which every client reads: Node's JSON.parse, which Electron
    clients use, rejects the mark and cannot read UTF-16 at all, so a
    marked file was already broken for most of them.

    Line endings come back as \\n, as read_text gave them: write_config
    writes in text mode, and a \\r\\n kept in the text would be written
    back as \\r\\r\\n on Windows.

    Raises OSError when the file cannot be read and UnicodeDecodeError (a
    ValueError) when it is not text in any of these encodings.
    """
    raw = Path(path).read_bytes()
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            text = raw.decode(codec)
            break
    else:
        text = raw.decode("utf-8")
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _read_json(path: Path) -> dict:
    """The file as a dict, or a refusal.

    An empty or missing file is a fresh start. Anything that will not parse
    is somebody's work that we do not understand, and the only safe move is
    to stop. So is JSON that is not an object (null, a list).
    """
    if not path.exists():
        return {}
    try:
        text = read_config_text(path).strip()
        data = json.loads(text) if text else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); "
                         "refusing to touch it") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; refusing to touch it")
    return data


# --- JSONC: the comments and trailing commas some clients allow ------------
#
# VS Code (and Insiders and VSCodium), Zed, Gemini CLI, Qwen Code and
# OpenCode read their config as JSON with comments: // and /* */, and a
# comma after the last member. Zed writes its own settings.json that way on
# first run, with a block of // lines above the opening brace, so a strict
# JSON reader refused every Zed user's file and printed the block instead.
#
# Re-serialising would parse, but it would delete every comment the user
# wrote. So the edit is textual: the document is parsed with the position
# of every object member, our member is spliced in (or out) as text, and
# the result is parsed again and must equal the original data with exactly
# our change applied. Anything this cannot do cleanly - a duplicate key, a
# root that is not an object, a result that does not read back - is
# refused, and the user gets the block to paste, as before.

_JSONC_SCALAR = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null")


class _JsoncObject(dict):
    """A parsed object that remembers where it is in the text: its braces,
    and for each member the start of its key and the span of its value."""

    def __init__(self, open_at: int):
        super().__init__()
        self.open = open_at
        self.close = -1
        self.spans: dict[str, tuple[int, int, int]] = {}


class _JsoncParser:
    def __init__(self, text: str):
        self.t, self.i = text, 0

    def fail(self, what: str):
        line = self.t.count("\n", 0, self.i) + 1
        raise ValueError(f"{what} at line {line}")

    def skip(self) -> None:
        t, n = self.t, len(self.t)
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif t.startswith("//", self.i):
                j = t.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif t.startswith("/*", self.i):
                j = t.find("*/", self.i + 2)
                if j < 0:
                    self.fail("unterminated comment")
                self.i = j + 2
            else:
                return

    def at(self, ch: str) -> bool:
        return self.t.startswith(ch, self.i)

    def document(self):
        self.skip()
        value = self.value()
        self.skip()
        if self.i != len(self.t):
            self.fail("text after the end of the document")
        return value

    def value(self):
        self.skip()
        if self.at("{"):
            return self.object()
        if self.at("["):
            return self.array()
        if self.at('"'):
            return self.string()
        m = _JSONC_SCALAR.match(self.t, self.i)
        if not m:
            self.fail("unexpected character")
        self.i = m.end()
        return json.loads(m.group())

    def string(self) -> str:
        try:
            s, self.i = json.decoder.scanstring(self.t, self.i + 1, True)
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
        return s

    def object(self) -> _JsoncObject:
        obj = _JsoncObject(self.i)
        self.i += 1
        while True:
            self.skip()
            if self.at("}"):
                break
            if not self.at('"'):
                self.fail("expected a quoted key")
            key_at = self.i
            key = self.string()
            self.skip()
            if not self.at(":"):
                self.fail("expected ':'")
            self.i += 1
            self.skip()
            value_at = self.i
            value = self.value()
            if key in obj:
                # Which one a client honours is the client's business; an
                # edit that guessed could change the one it reads.
                self.fail(f"duplicate key {key!r}")
            obj[key] = value
            obj.spans[key] = (key_at, value_at, self.i)
            self.skip()
            if self.at(","):
                self.i += 1
                continue
            if not self.at("}"):
                self.fail("expected ',' or '}'")
            break
        obj.close = self.i
        self.i += 1
        return obj

    def array(self) -> list:
        out = []
        self.i += 1
        while True:
            self.skip()
            if self.at("]"):
                break
            out.append(self.value())
            self.skip()
            if self.at(","):
                self.i += 1
                continue
            if not self.at("]"):
                self.fail("expected ',' or ']'")
            break
        self.i += 1
        return out


def _jsonc_parse(text: str):
    """The data in a JSONC document. Raises ValueError."""
    return _JsoncParser(text).document()


def _plain(value):
    """`value` as plain dicts and lists, for comparing and copying."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _line_start(text: str, i: int) -> int:
    return text.rfind("\n", 0, i) + 1


def _own_line_indent(text: str, i: int) -> str | None:
    """The indentation before position `i`, when nothing else is before it
    on its line."""
    lead = text[_line_start(text, i):i]
    return lead if not lead.strip() else None


def _line_indent(text: str, i: int) -> str:
    """The indentation of the line holding position `i`."""
    start = _line_start(text, i)
    line = text[start:]
    return line[:len(line) - len(line.lstrip(" \t"))]


def _indent_unit(text: str, root: _JsoncObject) -> str:
    """One level of indentation, as the document's own first member has."""
    for key_at, _, _ in sorted(root.spans.values()):
        lead = _own_line_indent(text, key_at)
        if lead:
            return lead
        break
    return "  "


def _jsonc_dump(value, indent: str, unit: str) -> str:
    """`value` as JSON, continuation lines indented to sit at `indent`."""
    return json.dumps(value, indent=unit).replace("\n", "\n" + indent)


def _jsonc_insert(text: str, obj: _JsoncObject, key: str, value,
                  unit: str) -> str:
    """`text` with `"key": value` added as the last member of `obj`."""
    if not obj.spans:
        close_indent = _line_indent(text, obj.open)
        member_indent = close_indent + unit
        member = f"{json.dumps(key)}: {_jsonc_dump(value, member_indent, unit)}"
        inside = text[obj.open + 1:obj.close]
        if not inside.strip():
            return (text[:obj.open + 1] + "\n" + member_indent + member
                    + "\n" + close_indent + text[obj.close:])
        return (text[:obj.open + 1] + "\n" + member_indent + member
                + text[obj.open + 1:])

    first_key = min(k for k, _, _ in obj.spans.values())
    _, _, last_end = max(obj.spans.values())
    member_indent = _own_line_indent(text, first_key)
    if member_indent is None:                   # a one-line object
        compact = json.dumps(value)
        return (text[:last_end] + f", {json.dumps(key)}: {compact}"
                + text[last_end:])
    member = f"{json.dumps(key)}: {_jsonc_dump(value, member_indent, unit)}"
    eol = text.find("\n", last_end)
    tail = text[last_end:] if eol < 0 else text[last_end:eol]
    after = tail.lstrip(" \t")
    trailing_comma = after.startswith(",")
    if trailing_comma:
        after = after[1:].lstrip(" \t")
    if eol >= 0 and (not after or after.startswith("//")):
        # The last member ends its line, perhaps with a comma or a comment
        # after it: the new member goes on a line of its own below.
        comma = "" if trailing_comma else ","
        return (text[:last_end] + comma + text[last_end:eol] + "\n"
                + member_indent + member + ("," if trailing_comma else "")
                + text[eol:])
    compact = json.dumps(value)
    return (text[:last_end] + f", {json.dumps(key)}: {compact}"
            + text[last_end:])


def _jsonc_set(text: str, root: str, name: str, entry: dict) -> str:
    """`text` with root[name] set to `entry`, every comment and every other
    byte left where it was. Raises ValueError when that cannot be done and
    proven."""
    doc = _jsonc_parse(text)
    if not isinstance(doc, _JsoncObject):
        raise ValueError("not a JSON object")
    unit = _indent_unit(text, doc)
    expected = _plain(doc)
    if root in doc:
        servers = doc[root]
        if not isinstance(servers, _JsoncObject):
            raise ValueError(f"{root} is not an object")
        if name in servers:
            key_at, value_at, value_end = servers.spans[name]
            indent = _line_indent(text, key_at)
            new = (text[:value_at] + _jsonc_dump(entry, indent, unit)
                   + text[value_end:])
        else:
            new = _jsonc_insert(text, servers, name, entry, unit)
        expected[root][name] = entry
    else:
        new = _jsonc_insert(text, doc, root, {name: entry}, unit)
        expected[root] = {name: entry}
    if _plain(_jsonc_parse(new)) != expected:
        raise ValueError("the edited file would not read back as intended")
    return new


def _skip_blank(text: str, i: int) -> int:
    """The first position at or after `i` that is not whitespace or a
    comment."""
    p = _JsoncParser(text)
    p.i = i
    p.skip()
    return p.i


def _jsonc_delete(text: str, root: str, name: str) -> str | None:
    """`text` with root[name] taken out and nothing else changed, or None
    when it is not there. Raises ValueError when it cannot be taken out
    cleanly."""
    doc = _jsonc_parse(text)
    if not isinstance(doc, _JsoncObject):
        raise ValueError("not a JSON object")
    servers = doc.get(root)
    if not isinstance(servers, _JsoncObject) or name not in servers:
        return None
    order = sorted(servers.spans, key=lambda k: servers.spans[k][0])
    idx = order.index(name)
    key_at, _, value_end = servers.spans[name]
    cuts: list[tuple[int, int]] = []

    def member_span(start: int, end: int) -> tuple[int, int]:
        """Widen [start, end) to whole lines when the member has its lines
        to itself."""
        eol = text.find("\n", end)
        rest = text[end:] if eol < 0 else text[end:eol]
        if _own_line_indent(text, start) is not None and not rest.strip():
            return _line_start(text, start), (len(text) if eol < 0
                                              else eol + 1)
        return start, end

    if idx < len(order) - 1:
        comma = _skip_blank(text, value_end)
        if not text.startswith(",", comma):
            raise ValueError("no comma after the entry")
        cuts.append(member_span(key_at, comma + 1))
    elif idx > 0:
        prev_end = servers.spans[order[idx - 1]][2]
        comma = _skip_blank(text, prev_end)
        if not text.startswith(",", comma):
            raise ValueError("no comma before the entry")
        end = value_end
        trailing = _skip_blank(text, value_end)
        if text.startswith(",", trailing):      # it had a trailing comma
            end = trailing + 1
        cuts.append((comma, comma + 1))
        cuts.append(member_span(key_at, end))
    else:
        end = value_end
        trailing = _skip_blank(text, value_end)
        if text.startswith(",", trailing):
            end = trailing + 1
        cuts.append(member_span(key_at, end))
    new = text
    for start, end in sorted(cuts, reverse=True):
        new = new[:start] + new[end:]
    expected = _plain(doc)
    del expected[root][name]
    if _plain(_jsonc_parse(new)) != expected:
        raise ValueError("the edited file would not read back as intended")
    return new


def write_config(path: Path, text: str) -> Path | None:
    """Replace a file the user or another program owns with `text`. Returns
    the byte-exact backup taken first, beside it, or None when there was no
    file yet.

    The text goes to a temporary file beside the file itself, flushed to
    disk, and is renamed over it, so a write cut short (a full disk, a kill)
    leaves the old file whole instead of truncated. A symlink is followed:
    the file it points at is replaced and the link stays a link. The file
    keeps its permission bits, and one this cannot write, or that sits in a
    directory this cannot write, is refused before anything is touched, as
    writing it in place would be.
    """
    from .vaultfile import _atomic_replace
    path = Path(path)
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    if not (os.access(target.parent, os.W_OK)
            and (not existed or os.access(target, os.W_OK))):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES),
                              str(target))
    backup = None
    if existed:
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        shutil.copy2(target, backup)
    tmp = target.with_name(f"{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if existed and os.name != "nt":
                shutil.copymode(target, tmp)    # before any text is in it
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(str(tmp), str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


#: TOML's short escapes; every other control character is \\uXXXX.
_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_string(s: str) -> str:
    """A TOML basic string.

    Not json.dumps, whose escaping looks like TOML's and is not: it writes a
    character outside the Basic Multilingual Plane as a UTF-16 surrogate
    pair, \\ud83d\\ude00, and TOML allows only Unicode scalar values in an
    escape. A vault in a folder named with an emoji, or with a CJK Extension
    B character in a user name, appended a table that made the whole of
    config.toml unreadable, and Codex refuses to start on a config it cannot
    read. Here only what TOML requires is escaped, and the rest is written
    as itself, in the UTF-8 the file is. A lone surrogate (an undecodable
    byte in a POSIX path) cannot be written in UTF-8 at all and raises
    ValueError.
    """
    out = []
    for ch in s:
        o = ord(ch)
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        elif 0xD800 <= o <= 0xDFFF:
            raise ValueError(f"{s!r} cannot be written as UTF-8 TOML")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(k: str) -> str:
    """A key, bare when TOML allows it bare."""
    return k if k and all(c.isascii() and (c.isalnum() or c in "-_")
                          for c in k) else _toml_string(k)


def _toml_value(v) -> str:
    """A TOML value for anything tomllib reads back: our own strings and
    string arrays, and whatever else the user put in our table (a
    startup_timeout_sec, an env table, enabled = false), which a refresh
    carries over."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "nan"
        if v in (float("inf"), float("-inf")):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}"
                                for k, x in v.items()) + " }"
    if hasattr(v, "isoformat"):                 # datetime, date, time
        return v.isoformat()
    return _toml_string(str(v))


def _toml_block(entry: dict) -> str:
    lines = [f"[mcp_servers.{NAME}]"]
    lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in entry.items()]
    return "\n".join(lines)


def _toml_has_ours(client: Client, text: str) -> bool:
    """Whether a TOML document defines our server, as TOML reads it rather
    than by the look of a line: a commented-out header is not one, a header
    with a comment after it is. Raises tomllib.TOMLDecodeError when the
    document does not parse."""
    servers = tomllib.loads(text).get(client.root)
    return isinstance(servers, dict) and NAME in servers


def _toml_statements(text: str) -> list[tuple[str, dict]] | None:
    """The document as its statements, each with what it parses to alone:
    a table header, a key and its value however many lines the value spans,
    a comment or a blank line. A line joins the statement before it until
    that parses, so a line inside a multi-line string or array is never
    taken for a header. None when the text ends inside a statement."""
    out, buf = [], ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        buf += line + ("\n" if i < len(lines) - 1 else "")
        if not buf:
            continue
        try:
            out.append((buf, tomllib.loads(buf)))
        except tomllib.TOMLDecodeError:
            continue
        buf = ""
    return None if buf else out


def _toml_header(statement: tuple[str, dict]) -> tuple[str, ...] | None:
    """The key path of a table header statement, None for anything else."""
    text, node = statement
    if not text.lstrip().startswith("["):
        return None
    path = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
    return tuple(path)


def _toml_without_ours(client: Client, text: str) -> str | None:
    """`text` with our table taken out: its header, every key in it, values
    over several lines included, and every [<root>.compartment.*] table
    under it. Comments and blank lines inside it go with it; the ones after
    its last key stay, since a comment just above the next table documents
    that table. Nothing else changes. None when that would not parse, or
    would still hold our server (an inline table, dotted keys), which is
    the check that matters more than any of the rest."""
    stmts = _toml_statements(text)
    if stmts is None:
        return None
    ours = (client.root, NAME)
    heads = [_toml_header(s) for s in stmts]
    keep = [True] * len(stmts)
    i = 0
    while i < len(stmts):
        if heads[i] is None or heads[i][:2] != ours:
            i += 1
            continue
        last, j = i, i + 1
        while j < len(stmts) and (heads[j] is None or heads[j][:2] == ours):
            if stmts[j][0].strip() and not stmts[j][0].lstrip().startswith("#"):
                last = j
            j += 1
        for k in range(i, last + 1):
            keep[k] = False
        # The blank line that set our table off goes with it when another
        # follows, so taking it out (or refreshing it) leaves one, not two.
        after = next((k for k in range(last + 1, len(stmts)) if keep[k]),
                     None)
        if (i > 0 and keep[i - 1] and not stmts[i - 1][0].strip()
                and (after is None or not stmts[after][0].strip())):
            keep[i - 1] = False
        i = j
    out = "".join(s for (s, _), k in zip(stmts, keep) if k)
    try:
        return None if _toml_has_ours(client, out) else out
    except tomllib.TOMLDecodeError:
        return None


def is_registered(client: Client, path: Path | None = None) -> bool:
    """Whether compartment is already in this client's server list."""
    p = Path(path) if path else client.config_path()
    if not p.exists():
        return False
    if client.kind == "toml":
        try:
            return _toml_has_ours(client, read_config_text(p))
        except (OSError, ValueError):    # unreadable, not UTF-8, not TOML
            return False
    if client.kind != "json":
        return False
    try:
        return NAME in (_read_json(p).get(client.root) or {})
    except ValueError:
        if not client.jsonc:
            return False
    try:
        doc = _jsonc_parse(read_config_text(p))
        servers = doc.get(client.root) if isinstance(doc, dict) else None
        return isinstance(servers, dict) and NAME in servers
    except (OSError, ValueError):
        return False


def register(client: Client, vault: str, caller: str | None = None,
             path: Path | None = None) -> dict:
    """Put compartment into one client's configuration.

    Returns what happened, so the caller can report it truthfully:
    `written` is False when the block was produced but not applied, and
    `snippet` is then what the user has to paste.
    """
    p = Path(path) if path else client.config_path()
    entry = entry_for(client, vault, caller)
    out = {"client": client.key, "display": client.display, "config": str(p),
           "backup": None, "written": False, "snippet": None,
           "note": client.note, "reason": None}

    if not client.writes:
        out["reason"] = "not a file this can safely edit"
        out["snippet"] = snippet(client, vault, caller)
        return out

    if client.kind == "toml":
        return _register_toml(client, p, entry, out)

    try:
        data = _read_json(p)
    except ValueError as exc:
        if client.jsonc:
            return _register_jsonc(client, p, entry, out, str(exc),
                                   snippet(client, vault, caller))
        out["reason"] = str(exc)
        out["snippet"] = snippet(client, vault, caller)
        return out

    servers = data.setdefault(client.root, {})
    if not isinstance(servers, dict):
        out["reason"] = f"{client.root} in {p} is not an object"
        out["snippet"] = snippet(client, vault, caller)
        return out
    servers[NAME] = entry                       # refresh rather than duplicate
    try:
        backup = write_config(p, json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        out["reason"] = f"cannot write {p} ({exc})"
        out["snippet"] = snippet(client, vault, caller)
        return out
    out["backup"] = str(backup) if backup else None
    out["written"] = True
    return out


def _register_jsonc(client: Client, p: Path, entry: dict, out: dict,
                    strict_error: str, block: str) -> dict:
    """Register in a file that is not strict JSON but that this client
    reads as JSON with comments: spliced in as text, every comment kept,
    and proven by reading it back. When it cannot be, the strict reader's
    refusal stands and the block is printed."""
    try:
        new = _jsonc_set(read_config_text(p), client.root, NAME, entry)
    except (OSError, ValueError) as exc:
        out["reason"] = (f"{strict_error}; nor could it be edited as JSON "
                         f"with comments ({exc})")
        out["snippet"] = block
        return out
    try:
        backup = write_config(p, new)
    except OSError as exc:
        out["reason"] = f"cannot write {p} ({exc})"
        out["snippet"] = block
        return out
    out["backup"] = str(backup) if backup else None
    out["written"] = True
    return out


def _toml_breaks(client: Client, before: str, after: str,
                 entry: dict) -> str | None:
    """Why `after` must not be written in place of `before`, or None.

    Appending a table is the whole edit, and an appended table is not
    always a valid one: a config that declares `mcp_servers = { ... }`
    inline, or as an array, cannot be extended by a [mcp_servers.x] header,
    and the result is a file Codex will not start with. So the document is
    read back as Codex will read it: it has to parse, hold exactly our
    entry, and hold everything else exactly as it did before."""
    try:
        doc = tomllib.loads(after)
        was = tomllib.loads(before)
    except tomllib.TOMLDecodeError as exc:
        return f"unreadable ({exc})"
    servers = doc.get(client.root)
    if not isinstance(servers, dict) or servers.get(NAME) != entry:
        return f"without a [{client.root}.{NAME}] table Codex can read"
    rest = dict(doc)
    rest[client.root] = {k: v for k, v in servers.items() if k != NAME}
    if not rest[client.root] and client.root not in was:
        del rest[client.root]
    return None if rest == was else "with its other settings changed"


def _register_toml(client: Client, p: Path, entry: dict, out: dict) -> dict:
    """Codex keeps TOML, and TOML tables are order-independent.

    So the whole edit is appending one table, which leaves every comment,
    every ordering and every hand-made formatting decision in the file
    exactly as its owner left it. Re-registering with the same command and
    arguments rewrites nothing. A file that does not parse is refused, as a
    JSON one is.

    Re-registering with a different command or vault refreshes the entry,
    as it does for every JSON client. It used to report "already
    registered; left as it is" whatever the entry said, so a config that
    named a bare `compartment` Codex could not find, a console script that
    an upgrade had moved, or last month's vault could not be put right by
    running `integrate codex` again: the user had to remove it first,
    which nothing told them. Our table is taken out exactly as
    `unregister` takes it out and appended again with the new command and
    arguments, and whatever else the user had put in it (a
    startup_timeout_sec, an env table, enabled = false) is carried over.
    When the table cannot be taken out cleanly (an inline entry, dotted
    keys), the file is left as it is and the block is printed.
    """
    try:
        block = _toml_block(entry)
    except ValueError as exc:
        out["reason"] = f"cannot be written as TOML ({exc})"
        out["snippet"] = json.dumps({client.root: {NAME: entry}}, indent=2)
        return out
    try:
        existing = read_config_text(p) if p.exists() else ""
        registered = _toml_has_ours(client, existing)
    except (OSError, UnicodeDecodeError) as exc:
        out["reason"] = f"cannot read {p} ({exc})"
        out["snippet"] = block
        return out
    except tomllib.TOMLDecodeError as exc:
        out["reason"] = f"{p} is not valid TOML ({exc}); refusing to touch it"
        out["snippet"] = block
        return out

    if registered:
        old = tomllib.loads(existing)[client.root][NAME]
        if not isinstance(old, dict) or all(old.get(k) == v
                                            for k, v in entry.items()):
            out["reason"] = "already registered; left as it is"
            out["snippet"] = block
            return out
        rest = _toml_without_ours(client, existing)
        if rest is None:
            out["reason"] = ("registered with another command, written in a "
                             "form that cannot be updated without breaking "
                             f"{p}; left as it is. Replace the entry with "
                             "this one")
            out["snippet"] = block
            return out
        entry = {**entry, **{k: v for k, v in old.items() if k not in entry}}
        try:
            refreshed = _toml_block(entry)
        except ValueError as exc:
            out["reason"] = f"cannot be written as TOML ({exc})"
            out["snippet"] = block
            return out
        before, verb = rest, "updating"
    else:
        before, refreshed, verb = existing, block, "adding"

    # One blank line before the table, however many times it is refreshed.
    joiner = ("" if not before or before.endswith("\n\n")
              else "\n" if before.endswith("\n") else "\n\n")
    text = before + joiner + refreshed + "\n"
    broken = _toml_breaks(client, before, text, entry)
    if broken:
        out["reason"] = (f"{verb} the table would leave {p} {broken}; "
                         "refusing to touch it")
        out["snippet"] = block
        return out
    try:
        backup = write_config(p, text)
    except OSError as exc:
        out["reason"] = f"cannot write {p} ({exc})"
        out["snippet"] = block
        return out
    out["backup"] = str(backup) if backup else None
    out["written"] = True
    return out


def unregister(client: Client, path: Path | None = None) -> bool:
    """Take compartment back out, and nothing else with it."""
    p = Path(path) if path else client.config_path()
    if not p.exists() or not client.writes:
        return False

    if client.kind == "toml":
        try:
            text = read_config_text(p)
            if not _toml_has_ours(client, text):
                return False
        except ValueError:          # not UTF-8 or not TOML: not ours to guess at
            return False
        rest = _toml_without_ours(client, text)
        if rest is None:            # left for the user to take out by hand
            return False
        write_config(p, rest)
        return True

    try:
        data = _read_json(p)
    except ValueError:
        if not client.jsonc:
            return False
        try:
            rest = _jsonc_delete(read_config_text(p), client.root, NAME)
        except (OSError, ValueError):   # left for the user to take out
            return False
        if rest is None:
            return False
        write_config(p, rest)
        return True
    servers = data.get(client.root) or {}
    if not isinstance(servers, dict) or NAME not in servers:
        return False
    del servers[NAME]
    data[client.root] = servers
    write_config(p, json.dumps(data, indent=2) + "\n")
    return True


def detected() -> list[Client]:
    """The clients that look installed on this machine."""
    return [c for c in CLIENTS.values() if c.present()]


def status() -> list[dict]:
    """One row per known client, for `integrate --list`."""
    rows = []
    for c in CLIENTS.values():
        here = c.present()
        rows.append({"key": c.key, "display": c.display, "present": here,
                     "registered": here and is_registered(c),
                     "writes": c.writes, "config": str(c.config_path())})
    return rows


__all__ = ["NAME", "CLIENTS", "ALIASES", "Client", "resolve", "executable",
           "server_args", "entry_for", "snippet", "is_registered", "register",
           "unregister", "detected", "status", "write_config"]
