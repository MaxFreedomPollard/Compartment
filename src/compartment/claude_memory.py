"""Import Claude Code's file-based memories into the vault.

Claude Code keeps a per-project memory directory of Markdown files, each
one a single fact with YAML-ish frontmatter:

    ---
    name: some-slug
    description: one-line summary
    metadata:
      type: user | feedback | project | reference
    ---
    the fact itself

That store is local to one project, unencrypted, and invisible to every
other agent and host. This module moves those facts into the vault, where
they are encrypted at rest and shared across every agent that speaks to
compartment. Nothing is deleted: the source files are read, never modified, so
an import is always safe to re-run.

Re-running is a no-op by design - Vault.store's near-duplicate guard
recognises text it has already embedded and returns the existing record
instead of a second copy.
"""
from __future__ import annotations

import os
import re
from pathlib import Path



def claude_config_dir() -> Path:
    """Claude Code's own directory: $CLAUDE_CONFIG_DIR when it is set,
    ~/.claude otherwise.

    Claude Code keeps everything there - settings.json, the per-project
    memory under projects/, skills/, the user CLAUDE.md - and moves all of
    it when CLAUDE_CONFIG_DIR is set ("Respect CLAUDE_CONFIG_DIR
    everywhere", Claude Code 1.0.6), which is how people run a second
    account or keep it in a dotfiles tree. Compartment only ever looked in
    ~/.claude, so for them the capture hook went into a settings.json
    Claude Code never read, the import found no memories, and the skill
    landed where no session would load it. Like Claude Code's own VS Code
    extension, only an absolute path counts; an empty or relative value is
    ignored."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
    return Path.home() / ".claude"


# Claude Code's per-project memory lives under <root>/<project-slug>/memory,
# and <root> is DEFAULT_ROOT. It is worked out when it is asked for, not at
# import: in a container run as an arbitrary uid, with HOME unset and no
# passwd entry, Path.home() raises, and computing it here killed every
# `compartment` command at import, including the ones that never touch
# Claude Code. A caller may still pin DEFAULT_ROOT by assigning it.


def _projects() -> Path:
    try:
        return claude_config_dir() / "projects"
    except RuntimeError:
        # No home directory to be found, so no Claude Code memory either:
        # a path that names nothing, for discovery to find nothing in and
        # `import-claude --help` to show, rather than an error.
        return Path("~") / ".claude" / "projects"


def default_root() -> Path:
    """Where Claude Code keeps per-project memory: DEFAULT_ROOT when it has
    been assigned, otherwise <Claude Code's directory>/projects, now."""
    pinned = globals().get("DEFAULT_ROOT")
    return Path(pinned) if pinned is not None else _projects()


def __getattr__(name: str):
    if name == "DEFAULT_ROOT":
        return _projects()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
INDEX_NAME = "MEMORY.md"          # a table of contents, not a fact

# Frontmatter `type` -> Compartment importance. Mirrors the vault's own tiers:
# who the user is and what they have decided outranks reference material.
IMPORTANCE = {
    "user": 0.85,
    "feedback": 0.85,
    "project": 0.75,
    "reference": 0.60,
}
DEFAULT_IMPORTANCE = 0.70

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def auto_memory_dir() -> Path | None:
    """The directory Claude Code's `autoMemoryDirectory` setting moves
    auto memory to, from the user settings.json beside DEFAULT_ROOT, or
    None when it is not set.

    Since Claude Code 2.1.74 that one setting replaces
    projects/<project>/memory/ with any directory the user names (~/
    expanded; it is ignored in a project's checked-in settings, so only
    the user's own file is read). Memory written there was invisible to
    both the import and the capture hook, which looked only under
    projects/. A relative value is ignored: there is no project here to
    resolve it against."""
    try:
        from .clients import _read_json
        value = _read_json(default_root().parent / "settings.json").get(
            "autoMemoryDirectory")
    except (OSError, ValueError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    d = Path(value.strip()).expanduser()
    return d if d.is_absolute() else None


def discover(root: Path | str | None = None) -> list[Path]:
    """Every Claude Code memory file, sorted, index files excluded: under
    `root`, or by default under DEFAULT_ROOT and in the directory
    autoMemoryDirectory names."""
    if root is None:
        out = discover(default_root())
        extra = auto_memory_dir()
        if extra is not None and extra.is_dir():
            out += [p for p in sorted(extra.glob("*.md"))
                    if p.name != INDEX_NAME and p.is_file() and p not in out]
        return out
    root = Path(root)
    if not root.exists():
        return []
    if root.name == "memory" and root.is_dir():      # a memory dir directly
        dirs = [root]
    else:
        dirs = sorted(p for p in root.glob("*/memory") if p.is_dir())
        if not dirs and (root / "memory").is_dir():
            dirs = [root / "memory"]
    out: list[Path] = []
    for d in dirs:
        out.extend(sorted(p for p in d.glob("*.md")
                          if p.name != INDEX_NAME and p.is_file()))
    return out


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (fields, body). Hand-rolled: the frontmatter Compartment cares
    about is flat scalars plus a one-level `metadata:` block, so pulling in
    a YAML dependency (and its parser CVEs) would be a poor trade."""
    m = _FRONTMATTER.match(raw)
    if not m:
        return {}, raw.strip()
    fields: dict[str, str] = {}
    section = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[:1].isspace()
        key, sep, val = line.strip().partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip().strip("'\"")
        if not val:                      # a nested block opens, e.g. metadata:
            section = key
            continue
        fields[f"{section}.{key}" if indented and section else key] = val
    return fields, raw[m.end():].strip()


def parse(path: Path) -> dict:
    """One memory file -> the record Compartment should store."""
    # utf-8-sig: a note saved by a Windows editor with a byte order mark
    # still opens with its frontmatter, instead of the mark hiding it and
    # the whole block being stored as the fact.
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    fields, body = _parse_frontmatter(raw)
    name = fields.get("name") or path.stem
    description = fields.get("description", "")
    kind = (fields.get("metadata.type") or fields.get("type") or "").lower()

    # The description is a standalone summary and the body is the fact; a
    # record carrying both retrieves well on either phrasing. Skip the
    # description when the body already opens with it.
    text = body
    if description and not body.lower().startswith(description.lower()[:40]):
        text = f"{description}\n\n{body}".strip() if body else description
    if not text.strip():
        text = description or name

    tags = ["claude-memory"]
    if kind:
        tags.append(kind)
    if name and name not in tags:
        tags.append(name)
    return {
        "text": text,
        "tags": tags,
        "importance": IMPORTANCE.get(kind, DEFAULT_IMPORTANCE),
        "name": name,
        "source": str(path),
    }


def file_tag(path: Path | str) -> str:
    """The tag naming the memory file a record came from, the same from the
    importer and the capture hook: "claude-file:<project>/<file>", where the
    project is the directory holding the memory directory. A later capture
    of the edited file finds by it what it replaces."""
    p = Path(path).absolute()
    memory = next((d for d in p.parents if d.name.lower() == "memory"),
                  p.parent)
    inside = p.relative_to(memory).as_posix()
    return f"claude-file:{memory.parent.name}/{inside}"


def import_files(vault, files: list[Path], caller: str = "import-claude",
                 namespace: str | None = None, dry_run: bool = False) -> dict:
    """Store each parsed memory. Returns counts plus per-file outcomes.

    Duplicates are not an error: re-importing an unchanged directory stores
    nothing new, so this is safe to run on every `compartment integrate claude`.
    """
    imported = duplicates = failed = 0
    items, errors = [], []
    for f in files:
        try:
            rec = parse(f)
        except OSError as exc:
            failed += 1
            errors.append(f"{f.name}: {exc}")
            continue
        tags = rec["tags"] + [file_tag(f)]
        if dry_run:
            imported += 1
            items.append({"name": rec["name"], "importance": rec["importance"],
                          "tags": tags, "chars": len(rec["text"])})
            continue
        try:
            out = vault.store(rec["text"], caller=caller, namespace=namespace,
                              tags=tags, importance=rec["importance"],
                              # Read off disk, not learned in conversation: the
                              # method has to say which of the two it was.
                              source=f"read from {f.name}",
                              # An import restores existing notes verbatim;
                              # the shape gate is for text being authored.
                              _gate=False)
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            errors.append(f"{f.name}: {exc}")
            continue
        if out.get("duplicate"):
            duplicates += 1
        else:
            imported += 1
        items.append({"name": rec["name"], "id": out.get("id"),
                      "duplicate": bool(out.get("duplicate"))})
    return {"scanned": len(files), "imported": imported,
            "duplicates": duplicates, "failed": failed,
            "dry_run": dry_run, "items": items, "errors": errors}


def pending(vault, root: Path | str | None = None) -> int:
    """How many Claude Code memory files are not in the vault yet.

    Cheap enough for `compartment status` to call: it compares file count against
    records already tagged `claude-memory`, no embedding involved.
    """
    files = discover(root)
    if not files:
        return 0
    try:
        rows = vault.db.conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE tags LIKE '%claude-memory%'"
        ).fetchone()
        have = int(rows["n"] if hasattr(rows, "keys") else rows[0])
    except Exception:                                   # noqa: BLE001
        return len(files)
    return max(0, len(files) - have)


def default_root_exists() -> bool:
    return default_root().exists() and bool(discover())


__all__ = ["DEFAULT_ROOT", "default_root", "discover", "parse", "import_files", "pending",
           "default_root_exists", "IMPORTANCE", "file_tag",
           "claude_config_dir", "auto_memory_dir"]
