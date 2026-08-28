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

# Claude Code's per-project memory lives under <root>/<project-slug>/memory.
DEFAULT_ROOT = Path.home() / ".claude" / "projects"
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


def discover(root: Path | str | None = None) -> list[Path]:
    """Every Claude Code memory file, sorted, index files excluded."""
    root = Path(root) if root else DEFAULT_ROOT
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
    raw = path.read_text(encoding="utf-8", errors="replace")
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
        if dry_run:
            imported += 1
            items.append({"name": rec["name"], "importance": rec["importance"],
                          "tags": rec["tags"], "chars": len(rec["text"])})
            continue
        try:
            out = vault.store(rec["text"], caller=caller, namespace=namespace,
                              tags=rec["tags"], importance=rec["importance"],
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
    return DEFAULT_ROOT.exists() and bool(discover())


__all__ = ["DEFAULT_ROOT", "discover", "parse", "import_files", "pending",
           "default_root_exists", "IMPORTANCE"]
