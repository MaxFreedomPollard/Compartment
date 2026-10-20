"""Where Compartment keeps its files, and what its environment is called.

Compartment was called engRAM until 1.15.0. Anyone who installed it before
then has a vault - possibly the only copy of everything their agents have
ever learned - sitting in `~/.engram`, along with their session state and the
downloaded embedding model. A rename that silently starts looking in
`~/.compartment` orphans all of it, and the failure looks like amnesia rather
than like a bug.

So the old directory keeps being used whenever it is the one that exists.
Nothing is moved, copied, or rewritten: the safest migration of a user's only
copy of their data is the one that does not touch it. A fresh install has no
`~/.engram` and gets `~/.compartment` as you would expect.

The same courtesy applies to the environment - `ENGRAM_VAULT` and its
siblings still work - and to individual files that carried the old name.
"""
from __future__ import annotations

import os
from pathlib import Path

NAME = "compartment"
LEGACY_NAME = "engram"


def home() -> Path:
    """The directory holding the vault, session state and models."""
    new = Path.home() / f".{NAME}"
    if new.exists():
        return new
    legacy = Path.home() / f".{LEGACY_NAME}"
    return legacy if legacy.exists() else new


def file(name: str, legacy: str | None = None) -> Path:
    """A path inside `home()`, preferring a legacy filename that exists."""
    here = home()
    current = here / name
    if legacy and not current.exists() and (here / legacy).exists():
        return here / legacy
    return current


def env(suffix: str, default: str | None = None) -> str | None:
    """`COMPARTMENT_<suffix>`, or the `ENGRAM_<suffix>` an existing setup may
    still be exporting from a shell profile or an MCP client config."""
    return (os.environ.get(f"{NAME.upper()}_{suffix}")
            or os.environ.get(f"{LEGACY_NAME.upper()}_{suffix}")
            or default)


def session_dir() -> Path:
    """Where the unlock credential and the per-boot markers live:
    COMPARTMENT_SESSION_DIR, else home()/session.

    The variable is read first and home() only when it is unset. Writing
    env("SESSION_DIR", home() / "session") evaluated home() every time, and
    in a container run as a uid with no passwd entry and no HOME,
    Path.home() raises: setting COMPARTMENT_SESSION_DIR, the documented way
    to put the session somewhere writable, could not get past it."""
    given = env("SESSION_DIR")
    return Path(given) if given else home() / "session"


__all__ = ["home", "file", "env", "session_dir", "NAME", "LEGACY_NAME"]
