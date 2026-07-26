"""Deterministic capture: mirror Claude Code memory writes into the vault.

Telling a model to prefer compartment is a request, and a host that declares its
own memory in the system prompt outranks anything a tool says. A hook does
not ask. When Claude Code writes a file into its memory directory, this hook
runs and the fact lands in the vault whether or not the model ever thought
about compartment.

Two halves:

* `install()` merges an compartment entry into ~/.claude/settings.json - additive,
  idempotent, and it backs the file up first. Other people's hooks are left
  exactly as they are; only compartment's own entries are replaced on re-install.
* `capture()` is what the hook command runs. It reads Claude Code's hook JSON
  on stdin, and if the written file is a memory file, stores it. It exits 0
  no matter what: a memory tool must never break the user's editor.
"""
from __future__ import annotations

from .home import env, home
import json
import os
import shutil
import sys
from pathlib import Path

from . import claude_memory

SETTINGS = Path.home() / ".claude" / "settings.json"
# Identifies OUR entries. Both tokens, not one literal string: the command
# carries `--vault ...` between them when a vault is pinned, so a single
# substring would stop matching and re-installing would duplicate the hook.
MARKER = "hook capture"
_OWNER = "compartment"
EVENT = "PostToolUse"
MATCHER = "Write|Edit|MultiEdit|NotebookEdit"
TIMEOUT = 15


def _entry(command: str) -> dict:
    return {"matcher": MATCHER,
            "hooks": [{"type": "command", "command": command,
                       "timeout": TIMEOUT}]}


def hook_command(compartment_bin: str | None = None, vault: str | None = None) -> str:
    """The exact shell command the hook runs."""
    exe = compartment_bin or shutil.which("compartment") or "compartment"
    if " " in exe:
        exe = f'"{exe}"'
    vault_part = f' --vault "{vault}"' if vault else ""
    return f"{exe}{vault_part} hook capture"


def is_ours(command: object) -> bool:
    """Whether a configured hook command belongs to compartment."""
    c = str(command or "")
    return MARKER in c and _OWNER in c


def is_installed(settings: Path | None = None) -> bool:
    data = _read(settings or SETTINGS)
    for group in (data.get("hooks", {}) or {}).get(EVENT, []) or []:
        for h in group.get("hooks", []) or []:
            if is_ours(h.get("command")):
                return True
    return False


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except (json.JSONDecodeError, OSError):
        raise ValueError(f"{path} is not valid JSON; refusing to touch it")


def install(compartment_bin: str | None = None, vault: str | None = None,
            settings: Path | None = None) -> dict:
    """Add (or refresh) compartment's capture hook. Never removes anyone else's."""
    path = Path(settings) if settings else SETTINGS
    data = _read(path)                                  # raises on malformed
    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".compartment-backup")
        shutil.copy2(path, backup)

    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault(EVENT, [])
    # drop only our own previous entries, so re-installing updates in place
    kept = []
    for group in groups:
        inner = [h for h in (group.get("hooks") or [])
                 if not is_ours(h.get("command"))]
        if inner:
            kept.append({**group, "hooks": inner})
        elif not group.get("hooks"):
            kept.append(group)
    kept.append(_entry(hook_command(compartment_bin, vault)))
    hooks[EVENT] = kept

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"settings": str(path), "backup": str(backup) if backup else None,
            "event": EVENT, "matcher": MATCHER}


def uninstall(settings: Path | None = None) -> bool:
    path = Path(settings) if settings else SETTINGS
    data = _read(path)
    groups = (data.get("hooks", {}) or {}).get(EVENT)
    if not groups:
        return False
    kept, removed = [], False
    for group in groups:
        inner = [h for h in (group.get("hooks") or [])
                 if not is_ours(h.get("command"))]
        if len(inner) != len(group.get("hooks") or []):
            removed = True
        if inner or not group.get("hooks"):
            kept.append({**group, "hooks": inner})
    if not removed:
        return False
    data["hooks"][EVENT] = kept
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


# ------------------------------------------------------------------ capture

def _file_from_payload(payload: dict) -> str | None:
    """Claude Code puts the edited path under tool_input; tolerate variants
    rather than silently capturing nothing if a field is renamed."""
    for key in ("tool_input", "toolInput", "input"):
        block = payload.get(key)
        if isinstance(block, dict):
            for f in ("file_path", "filePath", "path", "notebook_path"):
                if block.get(f):
                    return str(block[f])
    for f in ("file_path", "filePath", "path"):
        if payload.get(f):
            return str(payload[f])
    return None


def is_memory_file(path: str | Path) -> bool:
    """True for Claude Code's own memory notes - and nothing else. The index
    is a table of contents, so it is skipped like the importer skips it."""
    p = Path(path)
    if p.suffix.lower() != ".md" or p.name == claude_memory.INDEX_NAME:
        return False
    parts = [x.lower() for x in p.parts]
    return "memory" in parts and ".claude" in parts


def capture(stream=None, vault_path: str | None = None) -> dict:
    """Run by the hook. Returns a result dict; never raises, never blocks."""
    stream = stream or sys.stdin
    try:
        raw = stream.read()
    except Exception:                                   # noqa: BLE001
        return {"stored": False, "reason": "unreadable stdin"}
    if not raw or not raw.strip():
        return {"stored": False, "reason": "empty payload"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"stored": False, "reason": "payload is not JSON"}

    target = _file_from_payload(payload)
    if not target:
        return {"stored": False, "reason": "no file path in payload"}
    if not is_memory_file(target):
        return {"stored": False, "reason": "not a memory file"}
    p = Path(target)
    if not p.exists():
        return {"stored": False, "reason": "file no longer exists"}

    from .crypto import CryptoError
    from .vault import Vault
    vp = vault_path or env("VAULT", str(home() / "memory.vault"))
    if not os.path.exists(vp):
        return {"stored": False, "reason": "no vault"}
    try:
        pw, key = Vault.resolve_credential(vp)
        v = Vault.unlock(vp, passphrase=pw, raw_key=key)
    except CryptoError:
        # A locked vault is the user's choice, not an error worth shouting
        # about mid-edit; the next import-claude sweeps up whatever was missed.
        return {"stored": False, "reason": "vault locked"}
    try:
        rec = claude_memory.parse(p)
        out = v.store(rec["text"], caller="claude-code-hook",
                      tags=rec["tags"], importance=rec["importance"])
        v.save()
    except Exception as exc:                            # noqa: BLE001
        return {"stored": False, "reason": f"store failed: {exc}"}
    return {"stored": not out.get("duplicate"), "id": out.get("id"),
            "duplicate": bool(out.get("duplicate")), "name": rec["name"]}


__all__ = ["install", "uninstall", "is_installed", "is_ours", "capture", "hook_command",
           "is_memory_file", "SETTINGS", "MARKER", "EVENT", "MATCHER"]
