"""Install the /compartmentalize skill into every agent that reads skills.

A vault that is never written to is an empty vault. The MCP tools make storing
*possible*; a model still has to decide to do it, and the moment a long session
is summarized or compacted, everything it did not decide to store is gone. The
summary is produced by a pass that has no tools, so nothing can be saved from
inside it. The only place a save can happen is a turn before the summary, which
means somebody has to ask for one.

This is that ask, packaged. `compartmentalize` is a skill file written into the
agent's own skills directory, so the user types `/compartmentalize` and the
whole conversation is swept into the vault before the summary eats it.

Claude Code, Hermes and OpenClaw all read the same Agent Skills layout -
`<agent home>/skills/<name>/SKILL.md`, YAML frontmatter, markdown body - so one
packaged file serves all three and the only thing that differs is the root:

    claude    ~/.claude/skills/compartmentalize/SKILL.md
    hermes    $HERMES_HOME or ~/.hermes/skills/compartmentalize/SKILL.md
    openclaw  $OPENCLAW_HOME or ~/.openclaw/skills/compartmentalize/SKILL.md

Writing into somebody's skills directory is a larger claim on their machine
than registering an MCP server, so this module never does it silently and never
destroys an edited copy. A skill file that differs from the packaged one is
backed up before it is replaced, because an edited skill is the user's writing,
not ours to lose. `remove()` takes only what we put there.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Directory name, skill name and therefore the command the user types.
SKILL_NAME = "compartmentalize"
#: Written next to a skill we are about to replace, when it is not ours.
BACKUP_SUFFIX = ".compartment-backup"

#: Every agent known to read the Agent Skills layout, and the environment
#: variable that relocates its home. Claude Code has no such variable.
SKILL_TARGETS = {
    "claude": (None, Path.home() / ".claude"),
    "hermes": ("HERMES_HOME", Path.home() / ".hermes"),
    "openclaw": ("OPENCLAW_HOME", Path.home() / ".openclaw"),
}


def source() -> Path:
    """The packaged SKILL.md that ships inside the wheel."""
    return Path(__file__).resolve().parent / "data" / "agent-skill" / "SKILL.md"


def agent_home(target: str) -> Path | None:
    """Where the agent keeps its configuration, env override honoured."""
    entry = SKILL_TARGETS.get(target)
    if entry is None:
        return None
    var, default = entry
    if var and os.environ.get(var):
        return Path(os.environ[var]).expanduser()
    return default


def skill_path(target: str) -> Path | None:
    """Where this agent's copy of the skill belongs."""
    home = agent_home(target)
    return None if home is None else home / "skills" / SKILL_NAME / "SKILL.md"


def is_installed(target: str) -> bool:
    p = skill_path(target)
    return bool(p and p.is_file())


def is_current(target: str) -> bool:
    """Installed AND byte-identical to the packaged copy."""
    p = skill_path(target)
    if not (p and p.is_file()):
        return False
    try:
        return p.read_bytes() == source().read_bytes()
    except OSError:
        return False


def install(target: str) -> dict:
    """Write the skill for one agent.

    Returns {"action": ..., "path": str, "backup": str|None}. `action` is one
    of "unchanged" (already byte-identical), "written" (nothing was there),
    "updated" (ours, replaced) or "replaced" (someone had edited it, so the
    old text was backed up first). Raises OSError on a real filesystem
    failure - callers decide whether that is fatal, and at install time it
    never is.
    """
    src = source()
    if not src.is_file():                       # a broken wheel, not a no-op
        raise OSError(f"packaged skill missing at {src}")
    dst = skill_path(target)
    if dst is None:
        raise ValueError(f"no skills directory known for {target!r}")

    payload = src.read_bytes()
    backup = None
    action = "written"
    if dst.is_file():
        existing = dst.read_bytes()
        if existing == payload:
            return {"action": "unchanged", "path": str(dst), "backup": None}
        # Someone's edits are not ours to delete. Keep a copy, then update:
        # shipping a stale skill forever is the worse failure, and the backup
        # means the choice is still theirs.
        backup = dst.with_name(dst.name + BACKUP_SUFFIX)
        backup.write_bytes(existing)
        action = "replaced"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(payload)
    return {"action": action, "path": str(dst),
            "backup": str(backup) if backup else None}


def remove(target: str) -> bool:
    """Take back only what we wrote. True if something was removed."""
    dst = skill_path(target)
    if dst is None or not dst.is_file():
        return False
    try:
        dst.unlink()
    except OSError:
        return False
    # The directory is ours too - it carries the skill name - but only remove
    # it when it is empty, so a backup or anything the user added survives.
    try:
        dst.parent.rmdir()
    except OSError:
        pass
    return True


def status() -> dict:
    """Per-agent view, for `compartment status` and the app."""
    return {t: {"installed": is_installed(t), "current": is_current(t),
                "path": str(skill_path(t))}
            for t in SKILL_TARGETS}
