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

import json
import os
import shutil
import sys
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

    Cline, Roo Code and Kilo Code are VS Code extensions, so their settings
    live under VS Code's storage rather than anywhere of their own.
    """
    return _app_support(app) / "User"


def _ext_settings(ext: str, filename: str) -> Callable[[], Path]:
    """A VS Code extension's own settings file, by extension id."""
    return lambda: (_vscode_user() / "globalStorage" / ext / "settings"
                    / filename)


def _home(*parts: str) -> Callable[[], Path]:
    """A path under the user's home directory, the same on every platform."""
    return lambda: Path.home().joinpath(*parts)


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

    def config_path(self) -> Path:
        return self.path()

    def present(self) -> bool:
        """Whether this client looks installed.

        The config file itself often does not exist until the user adds their
        first server, so the directory it would live in is the real signal.
        """
        p = self.config_path()
        return p.exists() or p.parent.is_dir()


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
    Client("cline", "Cline",
           _ext_settings("saoudrizwan.claude-dev", "cline_mcp_settings.json"),
           note="reload the VS Code window to pick it up"),
    Client("roo", "Roo Code",
           _ext_settings("rooveterinaryinc.roo-cline", "mcp_settings.json"),
           aliases=("roo-code",),
           note="reload the VS Code window to pick it up"),
    Client("kilo", "Kilo Code",
           _ext_settings("kilocode.kilo-code", "mcp_settings.json"),
           aliases=("kilo-code",),
           note="reload the VS Code window to pick it up"),
    Client("gemini", "Gemini CLI", _home(".gemini", "settings.json"),
           aliases=("gemini-cli",)),
    Client("qwen", "Qwen Code CLI", _home(".qwen", "settings.json"),
           aliases=("qwen-code",)),
    Client("copilot-cli", "GitHub Copilot CLI",
           _home(".copilot", "mcp-config.json")),
    # Oh My Pi reads a project-level .omp/mcp.json as well, but that one is
    # relative to whatever repository you happen to be in, so the user-level
    # file is the only one an integrate command can honestly write.
    Client("omp", "Oh My Pi", _home(".omp", "agent", "mcp.json"),
           aliases=("oh-my-pi",),
           note="run `/mcp reload` in omp to pick it up without restarting"),

    # -- coding agents that spell the key differently ----------------------
    Client("vscode", "VS Code", lambda: _vscode_user() / "mcp.json",
           root="servers", entry=_entry_vscode),
    Client("vscode-insiders", "VS Code Insiders",
           lambda: _vscode_user("Code - Insiders") / "mcp.json",
           root="servers", entry=_entry_vscode),
    Client("zed", "Zed",
           lambda: (_appdata() / "Zed" / "settings.json" if os.name == "nt"
                    else _xdg() / "zed" / "settings.json"),
           root="context_servers",
           note="Zed allows comments in settings.json; if yours has any, "
                "the block is printed rather than written"),
    Client("opencode", "OpenCode",
           lambda: _xdg() / "opencode" / "opencode.json",
           root="mcp", entry=_entry_opencode),

    # -- Codex keeps TOML --------------------------------------------------
    Client("codex", "Codex CLI", _home(".codex", "config.toml"),
           root="mcp_servers", kind="toml", aliases=("codex-cli",)),

    # -- desktop and self-hosted chat apps ---------------------------------
    Client("lmstudio", "LM Studio", _home(".lmstudio", "mcp.json"),
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

    # -- known, documented, but not ours to write --------------------------
    #
    # Each of these keeps its configuration somewhere a merge cannot be made
    # safely: a YAML document whose comments and anchors we would flatten, or
    # an application database, or a settings blob with no stable on-disk
    # shape. Printing the exact block is honest; writing a guess is not.
    Client("continue", "Continue", _home(".continue", "config.yaml"),
           kind="manual", writes=False,
           note="paste under `mcpServers:` in config.yaml"),
    Client("goose", "Goose", lambda: _xdg() / "goose" / "config.yaml",
           kind="manual", writes=False,
           note="or run `goose configure` and add it as an extension"),
    Client("librechat", "LibreChat", lambda: Path("librechat.yaml"),
           kind="manual", writes=False,
           note="paste under `mcpServers:` in your librechat.yaml, then "
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
    `compartment` on it.
    """
    return shutil.which(NAME) or NAME


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
    return json.dumps({client.root: {NAME: entry_for(client, vault, caller)}},
                      indent=2)


# --- reading and writing ---------------------------------------------------

def _read_json(path: Path) -> dict:
    """The file as a dict, or a refusal.

    An empty or missing file is a fresh start. Anything that will not parse
    is somebody's work that we do not understand, and the only safe move is
    to stop.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); "
                         "refusing to touch it") from exc


def _backup(path: Path) -> Path | None:
    """A byte-exact copy beside the original, before we change it."""
    if not path.exists():
        return None
    dest = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    shutil.copy2(path, dest)
    return dest


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file, so an interrupted write cannot truncate
    somebody's configuration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _toml_value(v) -> str:
    """Just enough TOML to emit one table of strings and string arrays."""
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return json.dumps(str(v))          # JSON string escaping is TOML's too


def _toml_block(entry: dict) -> str:
    lines = [f"[mcp_servers.{NAME}]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in entry.items()]
    return "\n".join(lines)


def is_registered(client: Client, path: Path | None = None) -> bool:
    """Whether compartment is already in this client's server list."""
    p = Path(path) if path else client.config_path()
    if not p.exists():
        return False
    if client.kind == "toml":
        try:
            return f"[{client.root}.{NAME}]" in p.read_text(encoding="utf-8")
        except OSError:
            return False
    if client.kind != "json":
        return False
    try:
        return NAME in (_read_json(p).get(client.root) or {})
    except ValueError:
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
        out["reason"] = str(exc)
        out["snippet"] = snippet(client, vault, caller)
        return out

    out["backup"] = str(_backup(p) or "") or None
    servers = data.setdefault(client.root, {})
    if not isinstance(servers, dict):
        out["reason"] = f"{client.root} in {p} is not an object"
        out["snippet"] = snippet(client, vault, caller)
        return out
    servers[NAME] = entry                       # refresh rather than duplicate
    _write_atomic(p, json.dumps(data, indent=2) + "\n")
    out["written"] = True
    return out


def _register_toml(client: Client, p: Path, entry: dict, out: dict) -> dict:
    """Codex keeps TOML, and TOML tables are order-independent.

    So the whole edit is appending one table, which leaves every comment,
    every ordering and every hand-made formatting decision in the file
    exactly as its owner left it. Re-registering rewrites nothing: an entry
    that is already there is reported, not duplicated.
    """
    block = _toml_block(entry)
    try:
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        out["reason"] = f"cannot read {p} ({exc})"
        out["snippet"] = block
        return out

    if f"[{client.root}.{NAME}]" in existing:
        out["reason"] = "already registered; left as it is"
        out["snippet"] = block
        return out

    out["backup"] = str(_backup(p) or "") or None
    joiner = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    _write_atomic(p, existing + joiner + block + "\n")
    out["written"] = True
    return out


def unregister(client: Client, path: Path | None = None) -> bool:
    """Take compartment back out, and nothing else with it."""
    p = Path(path) if path else client.config_path()
    if not p.exists() or not client.writes:
        return False

    if client.kind == "toml":
        text = p.read_text(encoding="utf-8")
        header = f"[{client.root}.{NAME}]"
        if header not in text:
            return False
        # A TOML table runs until the next table header or the end of file.
        out_lines, dropping = [], False
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped == header:
                dropping = True
                continue
            if dropping and stripped.startswith("["):
                dropping = False
            if not dropping:
                out_lines.append(line)
        _backup(p)
        _write_atomic(p, "".join(out_lines))
        return True

    try:
        data = _read_json(p)
    except ValueError:
        return False
    servers = data.get(client.root) or {}
    if not isinstance(servers, dict) or NAME not in servers:
        return False
    del servers[NAME]
    _backup(p)
    data[client.root] = servers
    _write_atomic(p, json.dumps(data, indent=2) + "\n")
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
           "unregister", "detected", "status"]
