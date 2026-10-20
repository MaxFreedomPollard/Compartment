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
        A bare filename (LibreChat, Open WebUI) has no directory of its own:
        its parent is wherever the command runs, so only the file counts.
        """
        p = self.config_path()
        if not p.is_absolute():
            return p.exists()
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


def _toml_value(v) -> str:
    """Just enough TOML to emit one table of strings and string arrays."""
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return _toml_string(str(v))


def _toml_block(entry: dict) -> str:
    lines = [f"[mcp_servers.{NAME}]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in entry.items()]
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
    exactly as its owner left it. Re-registering rewrites nothing: an entry
    that is already there is reported, not duplicated. A file that does
    not parse is refused, as a JSON one is.
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
        out["reason"] = "already registered; left as it is"
        out["snippet"] = block
        return out

    joiner = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    text = existing + joiner + block + "\n"
    broken = _toml_breaks(client, existing, text, entry)
    if broken:
        out["reason"] = (f"adding the table would leave {p} {broken}; "
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
        return False
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
