"""Per-caller namespace access control + vault-adjacent settings.

Config lives NEXT to the vault as `<vault>.config.json` (it contains no
secrets - only ACLs and preferences). `packs/*` namespaces are ALWAYS
read-only for every caller, including "*". Violations are hard errors.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field

from .crypto import CryptoError

# The whole grant vocabulary. Anything outside it is not a grant at all, so it
# denies both reads and writes rather than being waved through as "not rw".
ALLOWED_GRANTS = ("rw", "ro", "none")
PERMITTED_GRANTS = ("rw", "ro")

DEFAULT_CONFIG = {
    "callers": {
        "*": {"default_namespace": "main", "grants": {"*": "rw"}},
    },
    "settings": {
        "auto_lock_minutes": 30,
        "include_packs_in_search": True,
        "search_starter_facts": True,
        "unlock_tool_enabled": False,
        # On: a memory stored with an expiry date is removed once that day has
        # passed. Off: the date is recorded and shown, and nothing is ever
        # deleted. Default on, because an expiry the user took the trouble to
        # set should do something.
        "expire_memories": True,
        "duplicate_threshold": 0.97,
        "index_precision": "f32",
    },
}


class AclError(CryptoError):
    pass


@dataclass
class VaultConfig:
    # Deep copies: a shallow copy would hand every VaultConfig in the process
    # the same inner grants dict, so editing one vault's ACLs would silently
    # edit the module default and every other open vault.
    callers: dict = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG["callers"]))
    settings: dict = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG["settings"]))

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
        # An entry that EXISTS is used as written, even when it is empty. Only
        # a missing caller falls back to "*". Writing {"evil": {}} is how a
        # caller gets locked down, so it must never inherit the wildcard.
        if caller in self.callers:
            entry = self.callers[caller]
        else:
            entry = self.callers.get("*")
        if not isinstance(entry, dict):
            raise AclError(f"Caller {caller!r} has no access to this vault")
        return entry

    def default_namespace(self, caller: str) -> str:
        return self._caller_entry(caller).get("default_namespace", "main")

    @staticmethod
    def _match(grants: dict, namespace: str):
        """The most specific grant for `namespace`, or None if nothing matches.

        Specificity, not dict order, decides. An exact namespace key beats
        every wildcard, and among wildcards the longest matching prefix wins,
        so "secret/*" beats "*" and "a/b/*" beats "a/*". Note that "*" is
        itself a wildcard whose prefix is empty: it is the weakest possible
        match, never the first one taken.
        """
        if not isinstance(grants, dict):
            return None
        if namespace in grants:
            return grants[namespace]
        best_len = -1
        best = None
        for pattern, g in grants.items():
            if not isinstance(pattern, str) or not pattern.endswith("*"):
                continue
            prefix = pattern[:-1]
            if namespace.startswith(prefix) and len(prefix) > best_len:
                best_len, best = len(prefix), g
        return best

    def grant_for(self, caller: str, namespace: str) -> str:
        """Returns 'rw' or 'ro'. Anything else raises AclError.

        'none' and any value outside the documented rw/ro/none vocabulary deny
        the namespace outright - reads included. packs/* is always ro.
        """
        entry = self._caller_entry(caller)
        grant = self._match(entry.get("grants", {}), namespace)
        if grant is None:
            raise AclError(
                f"Caller {caller!r} is not granted access to namespace {namespace!r}")
        if grant not in PERMITTED_GRANTS:
            if grant in ALLOWED_GRANTS:                  # explicit "none"
                raise AclError(
                    f"Caller {caller!r} is denied access to namespace "
                    f"{namespace!r}")
            raise AclError(
                f"Caller {caller!r} has an unrecognized grant {grant!r} for "
                f"namespace {namespace!r}; expected one of "
                f"{', '.join(ALLOWED_GRANTS)}")
        if namespace.startswith("packs/"):
            return "ro"  # pack namespaces are immutable for everyone
        return grant

    def check(self, caller: str, namespace: str, write: bool) -> None:
        # grant_for already denies reads for 'none' and for unknown values, so
        # this only has to police the read-only case on writes.
        grant = self.grant_for(caller, namespace)
        if write and grant != "rw":
            raise AclError(
                f"Caller {caller!r} has read-only access to namespace {namespace!r}")
