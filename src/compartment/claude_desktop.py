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
import shutil
import sys
from pathlib import Path

NAME = "compartment"
FILENAME = "claude_desktop_config.json"


def config_path() -> Path:
    """Claude Desktop's config file on this platform.

    The three locations are the app's own, not ours: macOS keeps it under
    Application Support, Windows under %APPDATA%, Linux under the XDG config
    directory.
    """
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Claude"
                / FILENAME)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData"
                                                / "Roaming")
        return Path(base) / "Claude" / FILENAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "Claude" / FILENAME


def present(path: Path | None = None) -> bool:
    """Whether Claude Desktop is on this machine at all.

    Its config file appears the first time it runs, so a freshly installed
    copy that has never been opened is recognised by its application
    directory instead. Writing a config for an app that is not installed
    would leave litter behind and connect nothing.
    """
    p = Path(path) if path else config_path()
    if p.exists() or p.parent.is_dir():
        return True
    if sys.platform == "darwin":
        return (Path("/Applications/Claude.app").exists()
                or (Path.home() / "Applications" / "Claude.app").exists())
    return False


def is_registered(path: Path | None = None) -> bool:
    """Whether compartment is in Claude Desktop's MCP server list."""
    p = Path(path) if path else config_path()
    try:
        return NAME in (_read(p).get("mcpServers") or {})
    except ValueError:              # malformed config - not registered
        return False


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except (json.JSONDecodeError, OSError):
        raise ValueError(f"{path} is not valid JSON; refusing to touch it")


def register(command: str, args: list[str], path: Path | None = None) -> dict:
    """Add (or refresh) compartment in Claude Desktop. Never removes anyone
    else's servers, and re-registering updates in place rather than
    duplicating."""
    p = Path(path) if path else config_path()
    data = _read(p)                                 # raises on malformed
    backup = None
    if p.exists():
        backup = p.with_suffix(p.suffix + ".compartment-backup")
        shutil.copy2(p, backup)

    servers = data.setdefault("mcpServers", {})
    servers[NAME] = {"command": command, "args": list(args)}

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
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
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return True


__all__ = ["config_path", "present", "is_registered", "register",
           "unregister", "NAME", "FILENAME"]
