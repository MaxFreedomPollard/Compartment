"""Per-caller namespace access control + vault-adjacent settings.

Config lives NEXT to the vault as `<vault>.config.json` (it contains no
secrets - only ACLs and preferences). `packs/*` namespaces are ALWAYS
read-only for every caller, including "*". Violations are hard errors.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .crypto import CryptoError

DEFAULT_CONFIG = {
    "callers": {
        "*": {"default_namespace": "main", "grants": {"*": "rw"}},
    },
    "settings": {
        "auto_lock_minutes": 30,
        "include_packs_in_search": True,
        "unlock_tool_enabled": False,
        "duplicate_threshold": 0.97,
        "index_precision": "f32",
    },
}


class AclError(CryptoError):
    pass


@dataclass
class VaultConfig:
    callers: dict = field(default_factory=lambda: DEFAULT_CONFIG["callers"].copy())
    settings: dict = field(default_factory=lambda: DEFAULT_CONFIG["settings"].copy())

    @staticmethod
    def path_for(vault_path: str) -> str:
        return vault_path + ".config.json"

    @classmethod
    def load(cls, vault_path: str) -> "VaultConfig":
        p = cls.path_for(vault_path)
        if not os.path.exists(p):
            return cls()
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls()
        cfg.callers = data.get("callers", cfg.callers)
        cfg.settings = {**cfg.settings, **data.get("settings", {})}
        return cfg

    def save(self, vault_path: str) -> None:
        with open(self.path_for(vault_path), "w", encoding="utf-8") as f:
            json.dump({"callers": self.callers, "settings": self.settings}, f, indent=2)

    # -- ACL ---------------------------------------------------------------

    def _caller_entry(self, caller: str) -> dict:
        entry = self.callers.get(caller) or self.callers.get("*")
        if entry is None:
            raise AclError(f"Caller {caller!r} has no access to this vault")
        return entry

    def default_namespace(self, caller: str) -> str:
        return self._caller_entry(caller).get("default_namespace", "main")

    def grant_for(self, caller: str, namespace: str) -> str:
        """Returns 'rw', 'ro', or raises AclError. packs/* is always ro."""
        entry = self._caller_entry(caller)
        grants = entry.get("grants", {})
        grant = grants.get(namespace)
        if grant is None:
            for pattern, g in grants.items():
                if pattern.endswith("*") and namespace.startswith(pattern[:-1]):
                    grant = g
                    break
        if grant is None:
            grant = grants.get("*")
        if grant is None:
            raise AclError(
                f"Caller {caller!r} is not granted access to namespace {namespace!r}")
        if namespace.startswith("packs/"):
            return "ro"  # pack namespaces are immutable for everyone
        return grant

    def check(self, caller: str, namespace: str, write: bool) -> None:
        grant = self.grant_for(caller, namespace)
        if write and grant != "rw":
            raise AclError(
                f"Caller {caller!r} has read-only access to namespace {namespace!r}")
