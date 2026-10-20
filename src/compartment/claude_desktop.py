"""Where Claude Desktop keeps its MCP servers, and how to register in one.

Claude Code and Claude Desktop are separate programs that read separate
configuration files. Wiring one says nothing at all about the other, so
`integrate claude` has to write both or it has not finished the job, and the
Connect button cannot report on an app it never looks at.

Printing a block for someone to paste is not connecting them. This module
exists so that it gets written.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import clients

NAME = "compartment"
FILENAME = "claude_desktop_config.json"

#: The MSIX package families Claude Desktop is published under on Windows:
#: the direct download (whose Claude Setup.exe has been an MSIX bootstrapper
#: since 2026, removing any older install) and the Microsoft Store build.
#: Taken from `Get-AppxPackage` output and the app's own main.log quoted in
#: anthropics/claude-ai-mcp#1150 and shraga100/claude-desktop-rtl-patch#45.
MSIX_FAMILIES = ("Claude_pzs8sxrjxfjjc", "AnthropicPBC.Claude_fnn82j28hfe8t")


def _windows() -> bool:
    return os.name == "nt"


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA")
                or Path.home() / "AppData" / "Local")


def _roaming_config() -> Path:
    """%APPDATA%\\Claude\\claude_desktop_config.json, the real one."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData"
                                            / "Roaming")
    return Path(base) / "Claude" / FILENAME


def _msix_configs() -> list[Path]:
    """The private config of each MSIX package of Claude Desktop installed
    for this user, installed meaning its package directory exists."""
    packages = _local_appdata() / "Packages"
    out = []
    for family in MSIX_FAMILIES:
        try:
            if (packages / family).is_dir():
                out.append(packages / family / "LocalCache" / "Roaming"
                           / "Claude" / FILENAME)
        except OSError:
            pass
    return out


def config_path() -> Path:
    """Claude Desktop's config file on this platform.

    The locations are the app's own, not ours: macOS keeps it under
    Application Support, Linux (the official beta since June 2026, and the
    community builds before it) under the XDG config directory, and Windows
    under %APPDATA% - except that an MSIX app does not write %APPDATA%.

    Claude Desktop on Windows is an MSIX package now, from the Store or
    from the direct download, and Windows redirects a packaged app's
    writes under %APPDATA% into the package's own
    %LOCALAPPDATA%\\Packages\\<family>\\LocalCache\\Roaming. The app reads
    that private copy first. Compartment is not inside the package, so it
    wrote the real %APPDATA%\\Claude file, which the app had stopped
    reading the first time it saved its settings: `integrate claude`
    reported Claude Desktop connected and the app never saw the entry.

    So with a package installed, its private copy is the file, once it
    exists. Until it does, the app still reads the real file through
    Windows' merged view, and that is the one written: a new private file
    holding only our server would hide every server already in the real
    one. With neither, the private path is created, which the app will
    read first. Of two packages with a private copy each, the one saved
    last is the one in use.
    """
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Claude"
                / FILENAME)
    if _windows():
        roaming = _roaming_config()
        packaged = _msix_configs()
        if not packaged:
            return roaming
        written = []
        for p in packaged:
            try:
                written.append((p.stat().st_mtime, p))
            except OSError:
                pass
        if written:
            return max(written)[1]
        return roaming if roaming.exists() else packaged[0]
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "Claude" / FILENAME


def present(path: Path | None = None) -> bool:
    """Whether Claude Desktop is on this machine at all.

    Its config file appears the first time it runs, so a freshly installed
    copy that has never been opened is recognised by its application
    directory instead. Writing a config for an app that is not installed
    would leave litter behind and connect nothing.

    On Windows that is an MSIX package directory, or the older installer's
    %LOCALAPPDATA%\\AnthropicClaude. A package that has never been opened
    has no %APPDATA%\\Claude to find, so it was reported as not installed
    and `integrate claude` skipped it.
    """
    p = Path(path) if path else config_path()
    if p.exists() or p.parent.is_dir():
        return True
    if sys.platform == "darwin":
        return (Path("/Applications/Claude.app").exists()
                or (Path.home() / "Applications" / "Claude.app").exists())
    if _windows() and path is None:
        try:
            return (bool(_msix_configs())
                    or (_local_appdata() / "AnthropicClaude").is_dir())
        except OSError:
            return False
    return False


def is_registered(path: Path | None = None) -> bool:
    """Whether compartment is in Claude Desktop's MCP server list."""
    p = Path(path) if path else config_path()
    try:
        return NAME in (_read(p).get("mcpServers") or {})
    except ValueError:              # malformed config - not registered
        return False


def _read(path: Path) -> dict:
    """The config as a dict, or ValueError: the same reader every other
    client's config goes through, so a file Notepad or PowerShell saved
    with a byte order mark, or as UTF-16, is read rather than refused."""
    return clients._read_json(path)


def register(command: str, args: list[str], path: Path | None = None) -> dict:
    """Add (or refresh) compartment in Claude Desktop. Never removes anyone
    else's servers, and re-registering updates in place rather than
    duplicating."""
    p = Path(path) if path else config_path()
    data = _read(p)                                 # raises on malformed

    servers = data.setdefault("mcpServers", {})
    servers[NAME] = {"command": command, "args": list(args)}

    # Backed up first, replaced whole, and a symlinked config stays a link.
    backup = clients.write_config(p, json.dumps(data, indent=2) + "\n")
    return {"config": str(p), "backup": str(backup) if backup else None}


def unregister(path: Path | None = None) -> bool:
    """Remove compartment from Claude Desktop. Returns whether it was there."""
    p = Path(path) if path else config_path()
    data = _read(p)
    servers = data.get("mcpServers") or {}
    if NAME not in servers:
        return False
    del servers[NAME]
    data["mcpServers"] = servers
    clients.write_config(p, json.dumps(data, indent=2) + "\n")
    return True


__all__ = ["config_path", "present", "is_registered", "register",
           "unregister", "NAME", "FILENAME"]
