"""Optional cross-platform desktop GUI for Compartment (macOS, Windows, Linux).

Compartment's status bar / tray / panel stays the default lightweight
companion. This module provides an optional full desktop workspace for
managing persistent encrypted documents (editing, versioning, diffs,
outlines, imports/exports, memory grounding and staleness review), atomic
memories, relations, settings, backups, and agent connections.

Design invariants:
* Zero new dependencies: built on Python's standard-library tkinter/ttk.
* Zero network sockets: works offline, including under --assert-offline.
* Zero plaintext on disk: decrypted content lives in RAM only unless the
  user explicitly invokes Export.
* Clean split: `GuiController` is pure Python with no tkinter import, so
  every vault, document, memory, relation, and security path is tested on
  every OS in CI without a display.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__, documents, session
from .acl import VaultConfig
from .crypto import CryptoError
from .gate import DEFAULT_MAX_CHARS as MAX_CHARS
from .menubar import (
    AUTO_LOCK_CHOICES,
    INTEGRATION_TARGETS,
    auto_lock_label,
    backup_vault,
    change_passphrase as _cli_change_passphrase,
    create_vault as _cli_create_vault,
    default_backup_filename,
    default_vault,
    integrate as _cli_integrate,
    integration_status as _cli_integration_status,
    lock_vault as _cli_lock_vault,
    merge_vault as _cli_merge_vault,
    open_dashboard,
    read_settings as _read_panel_settings,
    run_in_background,
    set_setting as _set_panel_setting,
    stop_dashboard,
    vault_signature,
)
from .vault import Vault, keychain_clear


#: Default caller name (matches CLI and VaultView so namespaces/counts align).
GUI_CALLER = "user"

#: Background check interval (seconds) for external lock or agent writes.
WATCH_INTERVAL_MS = 15_000


# ------------------------------------------------------------ pure controller

class GuiController:
    """Headless state and vault controller for the optional desktop GUI.

    Thread-safe via an RLock. Never imports tkinter; testable on every OS."""

    def __init__(self, vault_path: str | None = None, caller: str = GUI_CALLER):
        self.vault_path = os.path.abspath(
            os.path.expanduser(vault_path or default_vault()))
        self.caller = caller
        self._lock = threading.RLock()
        self._v: Vault | None = None
        self._vault_sig = None
        self._cred_sig = None

    # ---- lifecycle & security --------------------------------------------

    @property
    def exists(self) -> bool:
        return os.path.exists(self.vault_path)

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._v is not None

    def _drop_in_memory(self) -> None:
        v, self._v = self._v, None
        self._vault_sig = None
        self._cred_sig = None
        if v is not None:
            with contextlib.suppress(Exception):
                v.lock()

    def close(self) -> None:
        """Drop in-memory vault and key when closing the GUI window.

        Leaves the user's background session untouched so running agents and
        the top bar remain unaffected."""
        with self._lock:
            self._drop_in_memory()

    def _record_sigs(self) -> None:
        self._vault_sig = vault_signature(self.vault_path)
        self._cred_sig = session.watch_signature(self.vault_path)

    def try_open_from_session(self) -> tuple[bool, str]:
        """Open the vault silently if a valid unlocked session already exists."""
        with self._lock:
            if not self.exists:
                self._drop_in_memory()
                return False, "No vault found yet."
            try:
                found = Vault.find_credential(self.vault_path)
            except Exception as exc:                    # noqa: BLE001
                return False, f"Could not check session: {exc}"
            if found is None:
                self._drop_in_memory()
                return False, "Vault is locked."
            pw, key = found
            try:
                self._drop_in_memory()
                self._v = Vault.unlock(
                    self.vault_path, passphrase=pw, raw_key=key)
                self._record_sigs()
                return True, "Vault unlocked."
            except Exception as exc:                    # noqa: BLE001
                self._drop_in_memory()
                return False, str(exc)

    def unlock(self, passphrase: str) -> tuple[bool, str]:
        """Unlock the vault with a passphrase and publish the session key."""
        if not passphrase:
            return False, "Enter your passphrase."
        if not self.exists:
            return False, "No vault exists at this path."
        with self._lock:
            try:
                self._drop_in_memory()
                v = Vault.unlock(self.vault_path, passphrase=passphrase)
                session.store(self.vault_path, v._master)
                self._v = v
                self._record_sigs()
                return True, "Vault unlocked."
            except Exception as exc:                    # noqa: BLE001
                self._drop_in_memory()
                return False, str(exc)

    def create_vault(self, passphrase: str, repeat: str) -> tuple[bool, str]:
        """Create a new vault via the same safe stdin flow as the panel."""
        ok, msg = _cli_create_vault(self.vault_path, passphrase, repeat)
        if not ok:
            return False, msg
        opened, open_msg = self.try_open_from_session()
        if not opened:
            opened, open_msg = self.unlock(passphrase)
        return (True, msg) if opened else (False, open_msg)

    def lock(self) -> tuple[bool, str]:
        """Lock the vault everywhere (GUI RAM, session, keychain, dashboard)."""
        with self._lock:
            self._drop_in_memory()
            stop_dashboard()
            with contextlib.suppress(Exception):
                session.clear(self.vault_path)
            with contextlib.suppress(Exception):
                keychain_clear(self.vault_path)
            # Ensure CLI lock cleanup ran too (e.g. legacy session files).
            with contextlib.suppress(Exception):
                _cli_lock_vault(self.vault_path)
            return True, "Vault locked."

    def check_session_alive(self) -> bool:
        """Return False and drop RAM key if locked externally (e.g. CLI/top bar)."""
        with self._lock:
            if self._v is None:
                return False
            if not self.exists:
                self._drop_in_memory()
                return False
            sig = session.watch_signature(self.vault_path)
            if sig == self._cred_sig:
                return True
            try:
                found = Vault.find_credential(self.vault_path)
            except Exception:                           # noqa: BLE001
                return True
            if found is None or (
                found[1] is not None and self._v._master is not None
                and found[1] != self._v._master
            ):
                self._drop_in_memory()
                return False
            self._cred_sig = sig
            return True

    def sync_if_changed(self, *, force: bool = False) -> bool:
        """Reload in-memory vault seamlessly if another process wrote to disk.

        Returns True when a reload happened."""
        with self._lock:
            if self._v is None:
                return False
            cur_sig = vault_signature(self.vault_path)
            if not force and cur_sig == self._vault_sig:
                return False
            key = self._v._master
            if key is None:
                self._drop_in_memory()
                return False
            try:
                reloaded = Vault.unlock(self.vault_path, raw_key=key)
            except Exception:                           # noqa: BLE001
                ok, _ = self.try_open_from_session()
                return ok
            old = self._v
            self._v = reloaded
            self._record_sigs()
            with contextlib.suppress(Exception):
                old.lock()
            return True

    def _vault(self) -> Vault:
        if self._v is None:
            raise CryptoError("Vault is locked.")
        self.sync_if_changed()
        if self._v is None:
            raise CryptoError("Vault is locked.")
        return self._v

    def _after_write(self) -> None:
        self._record_sigs()

    # ---- overview & status -----------------------------------------------

    def summary(self) -> dict:
        """Full status payload for the header and Vault tab."""
        with self._lock:
            if not self.exists:
                return {
                    "exists": False, "locked": True,
                    "vault": self.vault_path,
                    "settings": self.read_settings(),
                    "integrations": self.integration_status(),
                }
            if self._v is None:
                return {
                    "exists": True, "locked": True,
                    "vault": self.vault_path,
                    "settings": self.read_settings(),
                    "integrations": self.integration_status(),
                }
            v = self._vault()
            st = v.status()
            st["exists"] = True
            st["locked"] = False
            st["settings"] = self.read_settings()
            st["integrations"] = self.integration_status()
            return st

    # ---- documents -------------------------------------------------------

    def list_docs(
        self,
        *,
        namespace: str | None = None,
        status: str = "active",
        collection: str | None = None,
        path_prefix: str | None = None,
        pinned_only: bool = False,
        stale_only: bool = False,
        sort_by: str = "updated",
        limit: int = 500,
    ) -> dict:
        with self._lock:
            v = self._vault()
            return v.doc_list(
                caller=self.caller,
                namespace=namespace or None,
                status=status,
                collection=collection or None,
                path_prefix=path_prefix or None,
                pinned_only=pinned_only,
                stale_memories_only=stale_only,
                sort_by=sort_by,
                limit=limit,
            )

    def search_docs(
        self,
        query: str,
        *,
        namespace: str | None = None,
        status: str = "active",
        collection: str | None = None,
        pinned_only: bool = False,
        top_k: int = 40,
    ) -> dict:
        with self._lock:
            v = self._vault()
            q = (query or "").strip()
            if not q:
                out = v.doc_list(
                    caller=self.caller, namespace=namespace or None,
                    status=status, collection=collection or None,
                    pinned_only=pinned_only, limit=top_k)
                return {"results": out["documents"], "total": out["total"]}
            res = v.doc_search(
                q, caller=self.caller, namespace=namespace or None,
                status=status, collection=collection or None,
                pinned_only=pinned_only, top_k=top_k)
            self._after_write()
            return res

    def get_doc(
        self,
        ref: str,
        *,
        version: int | None = None,
        section: str | None = None,
        max_chars: int = 200_000,
    ) -> dict:
        with self._lock:
            v = self._vault()
            return v.doc_get(
                ref, caller=self.caller, version=version, section=section,
                max_chars=max_chars, include_memories=True,
                include_history=True, include_binary=False, _audit=False)

    def save_doc(
        self,
        *,
        path: str,
        content: str,
        title: str | None = None,
        namespace: str | None = None,
        collection: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        status: str = "active",
        pinned: bool = False,
        change_note: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_put(
                path=path,
                doc_id=doc_id,
                content=content,
                title=title or None,
                namespace=namespace or None,
                collection=collection or None,
                tags=tags if tags is not None else [],
                summary=summary or None,
                source="compartment gui",
                status=status or "active",
                pinned=bool(pinned),
                change_note=change_note or None,
                caller=self.caller,
            )
            self._after_write()
            return res

    def patch_doc(
        self,
        ref: str,
        *,
        append: str | None = None,
        replace_old: str | None = None,
        replace_new: str | None = None,
        section: str | None = None,
        section_content: str | None = None,
        change_note: str | None = None,
    ) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_patch(
                ref,
                append=append,
                replace_old=replace_old,
                replace_new=replace_new,
                section=section,
                section_content=section_content,
                change_note=change_note,
                caller=self.caller,
            )
            self._after_write()
            return res

    def diff_doc(self, ref: str, from_version: int, to_version: int) -> dict:
        with self._lock:
            v = self._vault()
            return v.doc_diff(
                ref, int(from_version), int(to_version), caller=self.caller)

    def archive_doc(self, ref: str) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_delete(ref, caller=self.caller, archive=True)
            self._after_write()
            return res

    def delete_doc(self, ref: str, *, version: int | None = None,
                   shred: bool = False) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_delete(
                ref, caller=self.caller, version=version, shred=shred)
            self._after_write()
            return res

    def import_path(
        self,
        fs_path: str,
        *,
        namespace: str | None = None,
        prefix: str = "",
        collection: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_import_path(
                fs_path,
                caller=self.caller,
                namespace=namespace or None,
                prefix=prefix,
                collection=collection or None,
                tags=tags or None,
            )
            self._after_write()
            return res

    def export_doc(self, ref: str, dest_path: str,
                   *, version: int | None = None) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_export_file(
                ref, dest_path, caller=self.caller, version=version)
            self._after_write()
            return res

    def export_all(self, dest_dir: str, *, namespace: str | None = None,
                   include_archived: bool = False) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_export_all(
                dest_dir, caller=self.caller, namespace=namespace or None,
                include_archived=include_archived)
            self._after_write()
            return res

    def compile_memories(
        self,
        path: str,
        *,
        title: str | None = None,
        namespace: str | None = None,
        query: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> dict:
        with self._lock:
            v = self._vault()
            res = v.doc_compile(
                path, caller=self.caller, title=title or None,
                namespace=namespace or None, query=query or None,
                tags=tags or None, limit=int(limit))
            self._after_write()
            return res

    # ---- memories --------------------------------------------------------

    def search_memories(
        self,
        query: str,
        *,
        namespace: str | None = None,
        tags: list[str] | None = None,
        doc_id: str | None = None,
        top_k: int = 30,
        include_documents: bool = True,
    ) -> dict:
        with self._lock:
            v = self._vault()
            res = v.search(
                query, caller=self.caller, namespace=namespace or None,
                tags=tags or None, doc_id=doc_id or None, top_k=int(top_k),
                include_documents=include_documents)
            self._after_write()
            return res

    def recent_memories(
        self,
        *,
        namespace: str | None = None,
        limit: int = 50,
        include_seeded: bool = False,
    ) -> dict:
        with self._lock:
            v = self._vault()
            out = v.peek_recent(
                self.caller, namespace=namespace or None, limit=int(limit),
                include_seeded=include_seeded)
            out["results"] = list(reversed(out["results"]))
            return out

    def stale_memories(self, *, namespace: str | None = None) -> dict:
        with self._lock:
            v = self._vault()
            return v.doc_stale_memories(
                caller=self.caller, namespace=namespace or None)

    def store_memory(
        self,
        text: str,
        *,
        source: str = "from gui",
        kind: str = "fact",
        namespace: str | None = None,
        tags: list[str] | None = None,
        importance: float = 0.6,
        discovered: str | None = None,
        expires: str | None = None,
        doc_id: str | None = None,
        doc_section: str | None = None,
        supersedes: list[str] | None = None,
        keep_both: bool = False,
    ) -> dict:
        with self._lock:
            v = self._vault()
            eff_supersedes = ([] if (keep_both and supersedes is None)
                              else supersedes)
            res = v.store(
                text,
                caller=self.caller,
                namespace=namespace or None,
                tags=tags or None,
                importance=float(importance),
                source=(source or "from gui").strip(),
                discovered=discovered or None,
                expires=expires or None,
                kind=kind or "fact",
                doc_id=doc_id or None,
                doc_section=doc_section or None,
                supersedes=eff_supersedes,
            )
            self._after_write()
            return res

    def forget_memory(self, record_id: str, *, shred: bool = False) -> dict:
        with self._lock:
            v = self._vault()
            res = v.forget(record_id, caller=self.caller, shred=shred)
            self._after_write()
            return res

    # ---- relations -------------------------------------------------------

    def list_relations(
        self,
        *,
        entity: str | None = None,
        predicate: str | None = None,
        namespace: str | None = None,
        doc_id: str | None = None,
        limit: int = 200,
    ) -> dict:
        with self._lock:
            v = self._vault()
            out = v.relations(
                caller=self.caller,
                entity=entity or None,
                predicate=predicate or None,
                namespace=namespace or None,
                limit=int(limit),
            )
            if doc_id:
                out = {
                    **out,
                    "relations": [r for r in out.get("relations", [])
                                  if r.get("doc_id") == doc_id],
                }
            return out

    def add_relation(
        self,
        subject: str,
        predicate: str,
        obj: str,
        *,
        namespace: str | None = None,
        src_id: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        with self._lock:
            v = self._vault()
            res = v.link(
                subject, predicate, obj, caller=self.caller,
                namespace=namespace or None,
                src_id=src_id or None, doc_id=doc_id or None)
            self._after_write()
            return res

    def unlink_relation(self, rel_id: str) -> bool:
        with self._lock:
            v = self._vault()
            res = v.unlink(rel_id, caller=self.caller)
            self._after_write()
            return bool(res.get("removed")) if isinstance(res, dict) else bool(res)

    # ---- settings, agents, backups, maintenance --------------------------

    def read_settings(self) -> dict:
        base = _read_panel_settings(self.vault_path)
        cfg = VaultConfig.load(self.vault_path)
        base["search_documents_in_memory_search"] = bool(
            cfg.settings.get("search_documents_in_memory_search", True))
        return base

    def set_setting(self, key: str, value) -> dict:
        if key == "search_documents_in_memory_search":
            cfg = VaultConfig.load(self.vault_path)
            cfg.settings[key] = bool(value)
            cfg.save(self.vault_path)
            with self._lock:
                if self._v is not None:
                    self._v.config = VaultConfig.load(self.vault_path)
            return self.read_settings()
        _set_panel_setting(self.vault_path, key, value)
        with self._lock:
            if self._v is not None:
                self._v.config = VaultConfig.load(self.vault_path)
        return self.read_settings()

    def integration_status(self) -> dict:
        return _cli_integration_status(self.vault_path)

    def connect_agent(self, target: str) -> tuple[bool, str]:
        return _cli_integrate(self.vault_path, target)

    def change_passphrase(self, new_pw: str, repeat_pw: str) -> tuple[bool, str]:
        ok, msg = _cli_change_passphrase(self.vault_path, new_pw, repeat_pw)
        if ok:
            self.try_open_from_session()
        return ok, msg

    def backup(self, dest_path: str) -> tuple[bool, str]:
        return backup_vault(self.vault_path, dest_path)

    def merge(self, src_path: str, passphrase: str | None = None) -> tuple[bool, str, bool]:
        ok, msg, needs_pw = _cli_merge_vault(self.vault_path, src_path, passphrase)
        if ok:
            self.sync_if_changed(force=True)
        return ok, msg, needs_pw

    def export_jsonl(self, dest_path: str, *, include_relations: bool = True,
                     include_documents: bool = True) -> dict:
        with self._lock:
            v = self._vault()
            payload = v.export_jsonl(
                caller=self.caller,
                include_seeded=False,
                include_relations=include_relations,
                include_documents=include_documents,
            )
            dst = os.path.abspath(os.path.expanduser(dest_path))
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self._after_write()
            lines = sum(1 for ln in payload.splitlines() if ln.strip())
            return {"dest": dst, "lines": lines}

    def import_jsonl(self, src_path: str,
                     *, namespace: str | None = None) -> dict:
        with self._lock:
            v = self._vault()
            with open(os.path.abspath(os.path.expanduser(src_path)),
                      encoding="utf-8") as fh:
                text = fh.read()
            count = v.import_jsonl(
                text, caller=self.caller, namespace=namespace or None)
            self._after_write()
            return {"imported": count}

    def verify_audit(self) -> dict:
        from . import audit
        with self._lock:
            v = self._vault()
            ok, n, detail = audit.verify(v.db.conn)
            return {"ok": ok, "entries": n, "detail": detail,
                    "head": audit.head(v.db.conn)}

    def run_selftest(self) -> dict:
        from . import selftest
        with self._lock:
            v = self._vault()
            out = selftest.run(v)
            self._after_write()
            return out

    def expire_now(self) -> dict:
        with self._lock:
            v = self._vault()
            out = v.expire(caller=self.caller)
            self._after_write()
            return {"enabled": bool(out.get("enabled", True)),
                    "expired": int(out.get("removed", 0))}


def self_check(vault: str | None = None) -> int:
    """Headless verification for `compartment gui --self-check`."""
    ctrl = GuiController(vault)
    opened, msg = ctrl.try_open_from_session()
    try:
        st = ctrl.summary()
        print(f"vault:     {ctrl.vault_path}")
        print(f"exists:    {st.get('exists')}")
        print(f"locked:    {st.get('locked')}")
        if opened:
            print(f"memories:  {st.get('records', 0)} ({st.get('organic_records', 0)} organic)")
            print(f"documents: {st.get('documents', 0)} ({st.get('document_versions', 0)} versions)")
            print(f"grounded:  {st.get('grounded_memories', 0)} ({st.get('stale_memories', 0)} stale)")
        else:
            print(f"state:     {msg}")
        try:
            import tkinter  # noqa: F401
            print("toolkit:   tkinter ready")
        except ImportError:
            print("toolkit:   tkinter missing")
        return 0
    finally:
        ctrl.close()


# ----------------------------------------------------------- tkinter desktop

def _parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").replace(";", ",").split(",")
            if t.strip()]


def _format_size(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _build_window(root, ctrl: GuiController, *,
                  initial_doc_id: str | None = None):
    """Build the full Tk desktop workspace on `root`. Returns app handle dict."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from . import systray

    S = systray.ui_scale()
    if S != 1.0:
        with contextlib.suppress(tk.TclError):
            root.tk.call("tk", "scaling", 1.3333 * S)

    root.title("Compartment - Documents & Memory")
    w_init, h_init = int(1120 * S), int(740 * S)
    w_min, h_min = int(860 * S), int(580 * S)
    root.geometry(f"{w_init}x{h_init}")
    root.minsize(w_min, h_min)

    # Set window icon cleanly on every OS.
    if sys.platform == "win32" and systray.icon_path().is_file():
        with contextlib.suppress(Exception):
            root.iconbitmap(str(systray.icon_path()))
    elif systray.app_icon_path().is_file():
        with contextlib.suppress(Exception):
            _icon_img = tk.PhotoImage(file=str(systray.app_icon_path()))
            root.iconphoto(True, _icon_img)
            root._compartment_icon = _icon_img

    style = ttk.Style(root)
    if sys.platform not in ("darwin", "win32") and "clam" in style.theme_names():
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")

    ui_font = ("SF Pro Text", 12) if sys.platform == "darwin" else (
        ("Segoe UI", 10) if sys.platform == "win32" else ("TkDefaultFont", 10))
    mono_font = ("Menlo", 12) if sys.platform == "darwin" else (
        ("Consolas", 10) if sys.platform == "win32" else ("TkFixedFont", 10))
    bold_font = (ui_font[0], ui_font[1], "bold")
    title_font = (ui_font[0], ui_font[1] + 3, "bold")
    small_font = (ui_font[0], max(9, ui_font[1] - 1))

    app: dict = {
        "ctrl": ctrl,
        "root": root,
        "docs": [],
        "selected_doc": None,
        "doc_dirty": False,
        "memories": [],
        "relations": [],
        "closing": False,
        "busy": False,
        "watch_id": None,
        "widgets": {},
    }

    status_var = tk.StringVar(value="Ready - Local, Encrypted, Offline")
    summary_var = tk.StringVar(value="")

    def set_status(msg: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        status_var.set(f"{msg}  ({stamp})")

    def off_ui(work, done, *, busy_msg: str | None = None) -> None:
        if busy_msg:
            set_status(busy_msg)
        def _deliver(out):
            if app["closing"]:
                return
            with contextlib.suppress(tk.TclError):
                root.after(0, lambda: done(out))
        run_in_background(work, _deliver)

    main_host = ttk.Frame(root)
    main_host.pack(fill="both", expand=True)

    footer = ttk.Frame(root, padding=(10, 4))
    footer.pack(fill="x", side="bottom")
    ttk.Separator(root).pack(fill="x", side="bottom")
    ttk.Label(footer, textvariable=status_var, font=small_font).pack(side="left")
    ttk.Label(
        footer,
        text=f"Compartment v{__version__} · Local · Encrypted · Offline",
        font=small_font, foreground="#666",
    ).pack(side="right")

    def clear_host() -> None:
        for child in main_host.winfo_children():
            child.destroy()
        app["widgets"] = {}

    # ------------------------------------------------- Lock / Create screen

    def show_gate(note: str = "") -> None:
        clear_host()
        app["selected_doc"] = None
        app["doc_dirty"] = False
        outer = ttk.Frame(main_host, padding=36)
        outer.pack(expand=True)

        ttk.Label(outer, text="Compartment", font=(ui_font[0], ui_font[1] + 8, "bold")
                  ).pack(anchor="center", pady=(0, 4))
        ttk.Label(
            outer,
            text="Encrypted Documents & Atomic Memory Vault",
            font=ui_font, foreground="#666",
        ).pack(anchor="center", pady=(0, 6))
        ttk.Label(
            outer, text=ctrl.vault_path, font=small_font, foreground="#777"
        ).pack(anchor="center", pady=(0, 18))

        card = ttk.LabelFrame(
            outer,
            text="Unlock Vault" if ctrl.exists else "Create Encrypted Vault",
            padding=20,
        )
        card.pack(fill="x")

        msg_var = tk.StringVar(value=note)
        msg_lbl = ttk.Label(card, textvariable=msg_var, foreground="#b00020",
                            wraplength=int(360 * S), justify="left")

        if not ctrl.exists:
            ttk.Label(card, text="Choose passphrase:").pack(anchor="w")
            e1 = ttk.Entry(card, show="\u2022", width=36)
            e1.pack(fill="x", pady=(4, 10))
            ttk.Label(card, text="Repeat passphrase:").pack(anchor="w")
            e2 = ttk.Entry(card, show="\u2022", width=36)
            e2.pack(fill="x", pady=(4, 10))
            e1.focus_set()

            btn = ttk.Button(card, text="Create Vault")

            def do_create(*_):
                p1, p2 = e1.get(), e2.get()
                e1.delete(0, "end")
                e2.delete(0, "end")
                if not p1 or p1 != p2:
                    msg_var.set("Choose matching passphrases.")
                    return
                btn.configure(state="disabled")
                msg_var.set("Creating encrypted vault (Argon2id + starter pack)…")

                def done(out):
                    btn.configure(state="normal")
                    ok, m = (False, str(out)) if isinstance(out, Exception) else out
                    if ok:
                        show_workspace()
                        set_status("Vault created and unlocked.")
                    else:
                        msg_var.set(m)
                off_ui(lambda: ctrl.create_vault(p1, p2), done)

            btn.configure(command=do_create)
            btn.pack(anchor="w", pady=(4, 6))
            e1.bind("<Return>", do_create)
            e2.bind("<Return>", do_create)
            ttk.Label(
                card,
                text="There is no recovery phrase. If you forget your passphrase, "
                     "your documents and memories cannot be recovered.",
                font=small_font, foreground="#666", wraplength=int(360 * S),
                justify="left",
            ).pack(anchor="w", pady=(6, 0))
            msg_lbl.pack(anchor="w", pady=(8, 0))
        else:
            ttk.Label(card, text="Passphrase:").pack(anchor="w")
            pw_e = ttk.Entry(card, show="\u2022", width=36)
            pw_e.pack(fill="x", pady=(4, 10))
            pw_e.focus_set()

            btn_row = ttk.Frame(card)
            btn_row.pack(fill="x", pady=(2, 6))
            ubtn = ttk.Button(btn_row, text="Unlock")

            def do_unlock(*_):
                pw = pw_e.get()
                pw_e.delete(0, "end")
                if not pw:
                    msg_var.set("Enter your passphrase.")
                    return
                ubtn.configure(state="disabled")
                msg_var.set("Unlocking vault…")

                def done(out):
                    ubtn.configure(state="normal")
                    ok, m = (False, str(out)) if isinstance(out, Exception) else out
                    if ok:
                        show_workspace()
                        set_status("Vault unlocked.")
                    else:
                        msg_var.set(m)
                        pw_e.focus_set()
                off_ui(lambda: ctrl.unlock(pw), done)

            ubtn.configure(command=do_unlock)
            ubtn.pack(side="left")
            ttk.Button(
                btn_row, text="Back up…",
                command=lambda: prompt_backup(parent=root, on_msg=msg_var.set),
            ).pack(side="left", padx=(8, 0))
            pw_e.bind("<Return>", do_unlock)
            msg_lbl.pack(anchor="w", pady=(8, 0))

    # ---------------------------------------------- reusable action dialogs

    def prompt_backup(*, parent=None, on_msg=None) -> None:
        dest = filedialog.asksaveasfilename(
            parent=parent or root,
            title="Save Encrypted Vault Backup",
            initialfile=default_backup_filename(),
            defaultextension=".vault",
            filetypes=[("Encrypted Compartment Vault", "*.vault"),
                       ("All files", "*.*")])
        if not dest:
            return
        def done(out):
            ok, msg = (False, str(out)) if isinstance(out, Exception) else out
            set_status(msg)
            if on_msg:
                on_msg(msg)
            elif not ok:
                messagebox.showerror("Backup failed", msg, parent=root)
        off_ui(lambda: ctrl.backup(dest), done, busy_msg="Saving encrypted backup…")

    # ------------------------------------------------- main workspace UI

    def show_workspace(select_doc_id: str | None = None) -> None:
        clear_host()

        # ---- Top toolbar ----
        toolbar = ttk.Frame(main_host, padding=(12, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Compartment", font=title_font).pack(side="left")
        ttk.Label(toolbar, textvariable=summary_var, font=small_font,
                  foreground="#555").pack(side="left", padx=(12, 0))

        ttk.Button(toolbar, text="Lock", command=lambda: do_lock()
                   ).pack(side="right")
        ttk.Button(toolbar, text="Refresh", command=lambda: refresh_all(force=True)
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="Dashboard",
                   command=lambda: off_ui(lambda: open_dashboard(ctrl.vault_path),
                                          lambda u: set_status(
                                              "Opened browser dashboard." if u
                                              else "Could not start dashboard."))
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="Back up…", command=prompt_backup
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="Import File…", command=lambda: do_import_file()
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="+ New Doc", command=lambda: start_new_doc()
                   ).pack(side="right", padx=(0, 6))

        ttk.Separator(main_host).pack(fill="x")

        nb = ttk.Notebook(main_host)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        app["widgets"]["notebook"] = nb

        tab_docs = ttk.Frame(nb, padding=6)
        tab_mems = ttk.Frame(nb, padding=6)
        tab_rels = ttk.Frame(nb, padding=6)
        tab_vault = ttk.Frame(nb, padding=6)
        nb.add(tab_docs, text="  Documents  ")
        nb.add(tab_mems, text="  Memories  ")
        nb.add(tab_rels, text="  Relations  ")
        nb.add(tab_vault, text="  Vault & Agents  ")

        # ================================================= TAB 1: DOCUMENTS
        doc_pane = ttk.PanedWindow(tab_docs, orient="horizontal")
        doc_pane.pack(fill="both", expand=True)

        left_docs = ttk.Frame(doc_pane, padding=(0, 0, 6, 0))
        right_docs = ttk.Frame(doc_pane, padding=(6, 0, 0, 0))
        doc_pane.add(left_docs, weight=1)
        doc_pane.add(right_docs, weight=2)

        # Left search + filters
        ds_row = ttk.Frame(left_docs)
        ds_row.pack(fill="x", pady=(0, 6))
        doc_search_var = tk.StringVar()
        doc_search_entry = ttk.Entry(ds_row, textvariable=doc_search_var)
        doc_search_entry.pack(side="left", fill="x", expand=True)
        app["widgets"]["doc_search_entry"] = doc_search_entry
        ttk.Button(ds_row, text="Search", command=lambda: load_docs()
                   ).pack(side="left", padx=(4, 0))
        ttk.Button(ds_row, text="Clear",
                   command=lambda: (doc_search_var.set(""), load_docs())
                   ).pack(side="left", padx=(4, 0))
        doc_search_entry.bind("<Return>", lambda *_: load_docs())

        flt_row = ttk.Frame(left_docs)
        flt_row.pack(fill="x", pady=(0, 6))
        doc_status_var = tk.StringVar(value="active")
        ttk.Label(flt_row, text="Status:").pack(side="left")
        doc_status_cb = ttk.Combobox(
            flt_row, textvariable=doc_status_var,
            values=("active", "draft", "archived", "all"),
            state="readonly", width=9)
        doc_status_cb.pack(side="left", padx=(4, 8))
        doc_status_cb.bind("<<ComboboxSelected>>", lambda *_: load_docs())
        doc_pinned_var = tk.BooleanVar(value=False)
        doc_stale_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt_row, text="Pinned", variable=doc_pinned_var,
                        command=lambda: load_docs()).pack(side="left")
        ttk.Checkbutton(flt_row, text="Stale mem", variable=doc_stale_var,
                        command=lambda: load_docs()).pack(side="left", padx=(6, 0))

        # Document Treeview
        dtree_wrap = ttk.Frame(left_docs)
        dtree_wrap.pack(fill="both", expand=True)
        doc_cols = ("title", "path", "ver", "mem")
        doc_tree = ttk.Treeview(dtree_wrap, columns=doc_cols, show="headings",
                                selectmode="browse")
        doc_tree.heading("title", text="Title")
        doc_tree.heading("path", text="Path")
        doc_tree.heading("ver", text="Ver")
        doc_tree.heading("mem", text="Mem")
        doc_tree.column("title", width=int(140 * S), minwidth=80)
        doc_tree.column("path", width=int(130 * S), minwidth=80)
        doc_tree.column("ver", width=int(42 * S), anchor="center", stretch=False)
        doc_tree.column("mem", width=int(56 * S), anchor="center", stretch=False)
        dt_scroll = ttk.Scrollbar(dtree_wrap, orient="vertical",
                                  command=doc_tree.yview)
        doc_tree.configure(yscrollcommand=dt_scroll.set)
        doc_tree.pack(side="left", fill="both", expand=True)
        dt_scroll.pack(side="right", fill="y")

        doc_count_var = tk.StringVar(value="0 documents")
        left_foot = ttk.Frame(left_docs)
        left_foot.pack(fill="x", pady=(6, 0))
        ttk.Label(left_foot, textvariable=doc_count_var, font=small_font,
                  foreground="#666").pack(side="left")
        ttk.Button(left_foot, text="Compile Memories…",
                   command=lambda: open_compile_dialog()).pack(side="right")

        # Right side: metadata + action bar + sub-notebook
        meta_grid = ttk.Frame(right_docs)
        meta_grid.pack(fill="x", pady=(0, 6))
        for col in (1, 3):
            meta_grid.columnconfigure(col, weight=1)

        d_path_var = tk.StringVar()
        d_title_var = tk.StringVar()
        d_ns_var = tk.StringVar(value="main")
        d_col_var = tk.StringVar()
        d_tags_var = tk.StringVar()
        d_stat_var = tk.StringVar(value="active")
        d_pin_var = tk.BooleanVar(value=False)
        d_note_var = tk.StringVar()
        d_info_var = tk.StringVar(value="Select a document or click '+ New Doc'")

        ttk.Label(meta_grid, text="Path:").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(meta_grid, textvariable=d_path_var).grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=2)
        ttk.Label(meta_grid, text="Title:").grid(row=0, column=2, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(meta_grid, textvariable=d_title_var).grid(row=0, column=3, sticky="ew", pady=2)

        ttk.Label(meta_grid, text="Namespace:").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(meta_grid, textvariable=d_ns_var).grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=2)
        ttk.Label(meta_grid, text="Collection:").grid(row=1, column=2, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(meta_grid, textvariable=d_col_var).grid(row=1, column=3, sticky="ew", pady=2)

        ttk.Label(meta_grid, text="Tags:").grid(row=2, column=0, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(meta_grid, textvariable=d_tags_var).grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=2)
        ttk.Label(meta_grid, text="Status:").grid(row=2, column=2, sticky="w", padx=(0, 4), pady=2)
        st_pin = ttk.Frame(meta_grid)
        st_pin.grid(row=2, column=3, sticky="ew", pady=2)
        ttk.Combobox(st_pin, textvariable=d_stat_var,
                     values=("active", "draft", "archived"),
                     state="readonly", width=10).pack(side="left")
        ttk.Checkbutton(st_pin, text="Pinned", variable=d_pin_var
                        ).pack(side="left", padx=(8, 0))

        ttk.Label(meta_grid, text="Change note:").grid(row=3, column=0, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(meta_grid, textvariable=d_note_var).grid(row=3, column=1, columnspan=3, sticky="ew", pady=2)

        doc_acts = ttk.Frame(right_docs)
        doc_acts.pack(fill="x", pady=(2, 4))
        save_doc_btn = ttk.Button(doc_acts, text="Save Version",
                                  command=lambda: do_save_doc())
        save_doc_btn.pack(side="left")
        ttk.Button(doc_acts, text="Patch / Append…",
                   command=lambda: open_patch_dialog()).pack(side="left", padx=(6, 0))
        ttk.Button(doc_acts, text="Import Folder…",
                   command=lambda: do_import_folder()).pack(side="left", padx=(6, 0))
        ttk.Button(doc_acts, text="Export…",
                   command=lambda: do_export_doc()).pack(side="left", padx=(6, 0))
        ttk.Button(doc_acts, text="Archive",
                   command=lambda: do_archive_doc()).pack(side="left", padx=(6, 0))
        ttk.Button(doc_acts, text="Delete…",
                   command=lambda: do_delete_doc()).pack(side="left", padx=(6, 0))

        ttk.Label(right_docs, textvariable=d_info_var, font=small_font,
                  foreground="#555").pack(anchor="w", pady=(0, 4))

        doc_sub_nb = ttk.Notebook(right_docs)
        doc_sub_nb.pack(fill="both", expand=True)

        # Subtab 1: Editor / Content
        sub_content = ttk.Frame(doc_sub_nb, padding=4)
        doc_sub_nb.add(sub_content, text="Content")
        doc_editor = scrolledtext.ScrolledText(
            sub_content, wrap="word", font=mono_font, undo=True)
        doc_editor.pack(fill="both", expand=True)
        app["widgets"]["doc_editor"] = doc_editor

        def _mark_dirty(*_):
            if not app["doc_dirty"]:
                app["doc_dirty"] = True
                if not d_info_var.get().startswith("* "):
                    d_info_var.set("* " + d_info_var.get())

        doc_editor.bind("<KeyPress>", _mark_dirty)

        # Subtab 2: Outline
        sub_outline = ttk.Frame(doc_sub_nb, padding=4)
        doc_sub_nb.add(sub_outline, text="Outline")
        out_tree = ttk.Treeview(sub_outline, columns=("heading", "line"),
                                show="headings", selectmode="browse")
        out_tree.heading("heading", text="Section")
        out_tree.heading("line", text="Line")
        out_tree.column("heading", width=int(420 * S))
        out_tree.column("line", width=int(64 * S), anchor="center", stretch=False)
        out_scroll = ttk.Scrollbar(sub_outline, orient="vertical",
                                   command=out_tree.yview)
        out_tree.configure(yscrollcommand=out_scroll.set)
        out_tree.pack(side="left", fill="both", expand=True)
        out_scroll.pack(side="right", fill="y")

        def jump_to_outline(_event=None):
            sel = out_tree.selection()
            if not sel:
                return
            vals = out_tree.item(sel[0], "values")
            if vals:
                lineno = int(vals[1])
                doc_sub_nb.select(sub_content)
                doc_editor.see(f"{lineno}.0")
                doc_editor.mark_set("insert", f"{lineno}.0")
                doc_editor.focus_set()

        out_tree.bind("<Double-1>", jump_to_outline)

        # Subtab 3: History & Diff
        sub_hist = ttk.Frame(doc_sub_nb, padding=4)
        doc_sub_nb.add(sub_hist, text="History & Diff")
        h_top = ttk.Frame(sub_hist)
        h_top.pack(fill="x", pady=(0, 4))
        hist_tree = ttk.Treeview(
            h_top, columns=("ver", "date", "size", "sha", "note"),
            show="headings", height=6, selectmode="browse")
        for cid, txt, w in (
            ("ver", "Ver", 46), ("date", "Created", 136),
            ("size", "Size", 70), ("sha", "SHA-256", 96), ("note", "Note", 180),
        ):
            hist_tree.heading(cid, text=txt)
            hist_tree.column(cid, width=int(w * S))
        hist_tree.pack(side="left", fill="x", expand=True)

        diff_bar = ttk.Frame(sub_hist)
        diff_bar.pack(fill="x", pady=4)
        diff_from_var = tk.StringVar(value="1")
        diff_to_var = tk.StringVar(value="1")
        ttk.Label(diff_bar, text="From v:").pack(side="left")
        ttk.Entry(diff_bar, textvariable=diff_from_var, width=5).pack(side="left", padx=(4, 8))
        ttk.Label(diff_bar, text="To v:").pack(side="left")
        ttk.Entry(diff_bar, textvariable=diff_to_var, width=5).pack(side="left", padx=(4, 8))
        ttk.Button(diff_bar, text="Show Diff", command=lambda: do_show_diff()
                   ).pack(side="left")
        ttk.Button(diff_bar, text="View Selected Version",
                   command=lambda: do_load_selected_version()
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(diff_bar, text="Shred Selected Version",
                   command=lambda: do_shred_selected_version()
                   ).pack(side="left", padx=(6, 0))

        diff_text = scrolledtext.ScrolledText(
            sub_hist, wrap="none", font=mono_font, height=10)
        diff_text.pack(fill="both", expand=True)

        # Subtab 4: Grounded Memories
        sub_gmems = ttk.Frame(doc_sub_nb, padding=4)
        doc_sub_nb.add(sub_gmems, text="Grounded Memories")
        gmem_tree = ttk.Treeview(
            sub_gmems, columns=("state", "ver", "section", "text"),
            show="headings", selectmode="browse")
        gmem_tree.heading("state", text="State")
        gmem_tree.heading("ver", text="Ver")
        gmem_tree.heading("section", text="Section")
        gmem_tree.heading("text", text="Memory")
        gmem_tree.column("state", width=int(64 * S), anchor="center", stretch=False)
        gmem_tree.column("ver", width=int(42 * S), anchor="center", stretch=False)
        gmem_tree.column("section", width=int(120 * S))
        gmem_tree.column("text", width=int(320 * S))
        gmem_tree.pack(fill="both", expand=True, pady=(0, 6))

        gadd = ttk.LabelFrame(sub_gmems, text="Add Grounded Memory (<= 200 chars)",
                              padding=6)
        gadd.pack(fill="x")
        g_claim_var = tk.StringVar()
        g_sec_var = tk.StringVar()
        g_kind_var = tk.StringVar(value="fact")
        g_count_var = tk.StringVar(value=f"0/{MAX_CHARS}")
        g_claim_var.trace_add(
            "write", lambda *_: g_count_var.set(f"{len(g_claim_var.get())}/{MAX_CHARS}"))

        gr1 = ttk.Frame(gadd)
        gr1.pack(fill="x", pady=(0, 4))
        ttk.Entry(gr1, textvariable=g_claim_var).pack(side="left", fill="x", expand=True)
        ttk.Label(gr1, textvariable=g_count_var, font=small_font,
                  width=8).pack(side="left", padx=(6, 0))

        gr2 = ttk.Frame(gadd)
        gr2.pack(fill="x")
        ttk.Label(gr2, text="Section:").pack(side="left")
        ttk.Entry(gr2, textvariable=g_sec_var, width=18).pack(side="left", padx=(4, 8))
        ttk.Label(gr2, text="Kind:").pack(side="left")
        ttk.Combobox(gr2, textvariable=g_kind_var, values=("fact", "opinion"),
                     state="readonly", width=9).pack(side="left", padx=(4, 8))
        ttk.Button(gr2, text="Store Grounded Memory",
                   command=lambda: do_store_grounded_memory()).pack(side="left")
        ttk.Button(gr2, text="Forget Selected",
                   command=lambda: do_forget_grounded_memory()).pack(side="right")

        # ================================================= TAB 2: MEMORIES
        mem_top = ttk.Frame(tab_mems)
        mem_top.pack(fill="x", pady=(0, 6))
        mem_q_var = tk.StringVar()
        mem_q_entry = ttk.Entry(mem_top, textvariable=mem_q_var)
        mem_q_entry.pack(side="left", fill="x", expand=True)
        app["widgets"]["mem_search_entry"] = mem_q_entry
        mem_q_entry.bind("<Return>", lambda *_: load_memories("search"))

        ttk.Button(mem_top, text="Search",
                   command=lambda: load_memories("search")).pack(side="left", padx=(4, 0))
        ttk.Button(mem_top, text="Recent",
                   command=lambda: load_memories("recent")).pack(side="left", padx=(4, 0))
        ttk.Button(mem_top, text="Stale Only",
                   command=lambda: load_memories("stale")).pack(side="left", padx=(4, 0))
        ttk.Button(mem_top, text="+ Add Memory…",
                   command=lambda: open_add_memory_dialog()).pack(side="left", padx=(8, 0))

        mem_pane = ttk.PanedWindow(tab_mems, orient="vertical")
        mem_pane.pack(fill="both", expand=True)

        m_list_frame = ttk.Frame(mem_pane)
        m_det_frame = ttk.Frame(mem_pane, padding=(0, 6, 0, 0))
        mem_pane.add(m_list_frame, weight=3)
        mem_pane.add(m_det_frame, weight=2)

        mem_tree = ttk.Treeview(
            m_list_frame, columns=("kind", "score", "date", "doc", "text"),
            show="headings", selectmode="browse")
        for cid, txt, w in (
            ("kind", "Kind", 68), ("score", "Score", 62),
            ("date", "Date", 132), ("doc", "Source Doc", 160), ("text", "Memory", 540),
        ):
            mem_tree.heading(cid, text=txt)
            mem_tree.column(cid, width=int(w * S))
        m_scroll = ttk.Scrollbar(m_list_frame, orient="vertical",
                                 command=mem_tree.yview)
        mem_tree.configure(yscrollcommand=m_scroll.set)
        mem_tree.pack(side="left", fill="both", expand=True)
        m_scroll.pack(side="right", fill="y")

        m_act = ttk.Frame(m_det_frame)
        m_act.pack(fill="x", pady=(0, 4))
        m_open_doc_btn = ttk.Button(
            m_act, text="Open Source Document", state="disabled",
            command=lambda: open_selected_memory_doc())
        m_open_doc_btn.pack(side="left")
        ttk.Button(m_act, text="Forget",
                   command=lambda: do_forget_selected_memory(shred=False)
                   ).pack(side="right")
        ttk.Button(m_act, text="Crypto-Shred…",
                   command=lambda: do_forget_selected_memory(shred=True)
                   ).pack(side="right", padx=(0, 6))

        mem_detail = scrolledtext.ScrolledText(
            m_det_frame, wrap="word", font=ui_font, height=6)
        mem_detail.pack(fill="both", expand=True)
        mem_detail.configure(state="disabled")

        # ================================================= TAB 3: RELATIONS
        rel_top = ttk.Frame(tab_rels)
        rel_top.pack(fill="x", pady=(0, 6))
        rel_ent_var = tk.StringVar()
        ttk.Label(rel_top, text="Entity:").pack(side="left")
        rel_ent_e = ttk.Entry(rel_top, textvariable=rel_ent_var, width=24)
        rel_ent_e.pack(side="left", padx=(4, 6))
        rel_ent_e.bind("<Return>", lambda *_: load_relations())
        ttk.Button(rel_top, text="Filter", command=lambda: load_relations()
                   ).pack(side="left")
        ttk.Button(rel_top, text="Show All",
                   command=lambda: (rel_ent_var.set(""), load_relations())
                   ).pack(side="left", padx=(4, 0))
        ttk.Button(rel_top, text="Unlink Selected",
                   command=lambda: do_unlink_relation()).pack(side="right")

        rel_tree = ttk.Treeview(
            tab_rels, columns=("subject", "predicate", "object", "ns", "doc"),
            show="headings", selectmode="browse")
        for cid, txt, w in (
            ("subject", "Subject", 200), ("predicate", "Predicate", 150),
            ("object", "Object", 220), ("ns", "Namespace", 90), ("doc", "Doc ID", 110),
        ):
            rel_tree.heading(cid, text=txt)
            rel_tree.column(cid, width=int(w * S))
        rel_tree.pack(fill="both", expand=True, pady=(0, 6))

        rel_add = ttk.LabelFrame(tab_rels, text="Add Relation", padding=6)
        rel_add.pack(fill="x")
        rs_var, rp_var, ro_var, rns_var = (
            tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar(value="main"))
        for lbl, var, w in (
            ("Subject:", rs_var, 18), ("Predicate:", rp_var, 14),
            ("Object:", ro_var, 20), ("NS:", rns_var, 8),
        ):
            ttk.Label(rel_add, text=lbl).pack(side="left")
            ttk.Entry(rel_add, textvariable=var, width=w).pack(side="left", padx=(4, 8))
        ttk.Button(rel_add, text="Link", command=lambda: do_add_relation()
                   ).pack(side="left")

        # ================================================= TAB 4: VAULT & AGENTS
        v_cols = ttk.Frame(tab_vault)
        v_cols.pack(fill="both", expand=True)
        v_cols.columnconfigure(0, weight=1)
        v_cols.columnconfigure(1, weight=1)

        v_left = ttk.Frame(v_cols, padding=(0, 0, 8, 0))
        v_right = ttk.Frame(v_cols, padding=(8, 0, 0, 0))
        v_left.grid(row=0, column=0, sticky="nsew")
        v_right.grid(row=0, column=1, sticky="nsew")

        v_info_box = ttk.LabelFrame(v_left, text="Vault Status", padding=10)
        v_info_box.pack(fill="x", pady=(0, 8))
        v_info_text = tk.StringVar(value="Loading…")
        ttk.Label(v_info_box, textvariable=v_info_text, justify="left",
                  font=mono_font).pack(anchor="w")

        v_ops = ttk.LabelFrame(v_left, text="Backup, Sync & Maintenance", padding=10)
        v_ops.pack(fill="x")
        ops_r1 = ttk.Frame(v_ops)
        ops_r1.pack(fill="x", pady=(0, 6))
        ttk.Button(ops_r1, text="Back up .vault…", command=prompt_backup
                   ).pack(side="left")
        ttk.Button(ops_r1, text="Merge .vault…", command=lambda: do_merge_vault()
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(ops_r1, text="Change Password…",
                   command=lambda: open_change_pw_dialog()).pack(side="left", padx=(6, 0))

        ops_r2 = ttk.Frame(v_ops)
        ops_r2.pack(fill="x", pady=(0, 6))
        ttk.Button(ops_r2, text="Export All Docs…", command=lambda: do_export_all()
                   ).pack(side="left")
        ttk.Button(ops_r2, text="Export JSONL…", command=lambda: do_export_jsonl()
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(ops_r2, text="Import JSONL…", command=lambda: do_import_jsonl()
                   ).pack(side="left", padx=(6, 0))

        ops_r3 = ttk.Frame(v_ops)
        ops_r3.pack(fill="x")
        ttk.Button(ops_r3, text="Verify Audit Chain",
                   command=lambda: do_verify_audit()).pack(side="left")
        ttk.Button(ops_r3, text="Run Selftest",
                   command=lambda: do_selftest()).pack(side="left", padx=(6, 0))
        ttk.Button(ops_r3, text="Expire Old Memories",
                   command=lambda: do_expire_now()).pack(side="left", padx=(6, 0))

        # Settings card
        s_box = ttk.LabelFrame(v_right, text="Settings", padding=10)
        s_box.pack(fill="x", pady=(0, 8))
        set_vars = {
            "capture_hook": tk.BooleanVar(),
            "search_starter_facts": tk.BooleanVar(),
            "expire_memories": tk.BooleanVar(),
            "search_documents_in_memory_search": tk.BooleanVar(),
        }

        def apply_setting(key, val):
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Setting error", str(out), parent=root)
                else:
                    set_status(f"Saved setting {key}.")
            off_ui(lambda: ctrl.set_setting(key, val), done)

        for key, lbl in (
            ("capture_hook", "Create memories automatically (Claude hook)"),
            ("search_starter_facts", "Search starter facts"),
            ("expire_memories", "Forget memories when they expire"),
            ("search_documents_in_memory_search",
             "Surface matching documents in memory_search"),
        ):
            ttk.Checkbutton(
                s_box, text=lbl, variable=set_vars[key],
                command=lambda k=key: apply_setting(k, bool(set_vars[k].get())),
            ).pack(anchor="w", pady=2)

        al_row = ttk.Frame(s_box)
        al_row.pack(fill="x", pady=(6, 0))
        ttk.Label(al_row, text="Auto-lock idle timeout:").pack(side="left")
        al_var = tk.StringVar(value=auto_lock_label(30))

        def on_autolock(_e=None):
            chosen = al_var.get()
            for mins in AUTO_LOCK_CHOICES:
                if auto_lock_label(mins) == chosen:
                    apply_setting("auto_lock_minutes", mins)
                    break

        al_cb = ttk.Combobox(
            al_row, textvariable=al_var,
            values=[auto_lock_label(m) for m in AUTO_LOCK_CHOICES],
            state="readonly", width=10)
        al_cb.pack(side="left", padx=(8, 0))
        al_cb.bind("<<ComboboxSelected>>", on_autolock)

        # Agents card
        a_box = ttk.LabelFrame(v_right, text="Connect an Agent", padding=10)
        a_box.pack(fill="x")
        agent_btns: dict[str, ttk.Button] = {}
        agent_note_var = tk.StringVar(value="")

        def do_connect(target, name):
            agent_note_var.set(f"Connecting {name}…")
            def done(out):
                ok, msg = (False, str(out)) if isinstance(out, Exception) else out
                agent_note_var.set(msg)
                set_status(msg)
                refresh_summary()
            off_ui(lambda: ctrl.connect_agent(target), done)

        arow = ttk.Frame(a_box)
        arow.pack(fill="x")
        for target, name in INTEGRATION_TARGETS:
            b = ttk.Button(
                arow, text=name,
                command=lambda t=target, n=name: do_connect(t, n))
            b.pack(side="left", padx=(0, 6))
            agent_btns[target] = b
        ttk.Label(a_box, textvariable=agent_note_var, wraplength=int(420 * S),
                  foreground="#555", justify="left").pack(anchor="w", pady=(8, 0))

        # ============================================ DATA LOADING & ACTIONS

        def render_summary(st: dict) -> None:
            if st.get("locked"):
                show_gate("Vault was locked.")
                return
            recs = int(st.get("records") or 0)
            org = int(st.get("organic_records") or 0)
            docs = int(st.get("documents") or 0)
            stale = int(st.get("stale_memories") or 0)
            parts = [f"{recs:,} memories ({org:,} organic)",
                     f"{docs:,} {'doc' if docs == 1 else 'docs'}"]
            if stale > 0:
                parts.append(f"{stale:,} stale")
            summary_var.set(" · ".join(parts))

            lines = [
                f"Vault:     {st.get('vault', ctrl.vault_path)}",
                f"Model:     {st.get('model', '-')}",
                f"Memories:  {recs:,} total ({org:,} organic, {st.get('seeded_records', 0):,} seeded)",
                f"Documents: {docs:,} active ({st.get('document_versions', 0):,} versions)",
                f"Grounded:  {st.get('grounded_memories', 0):,} memories ({stale:,} stale)",
                f"Relations: {st.get('relations', 0):,}",
                f"Est. RAM:  ~{st.get('projected_ram_mb', 0)} MB",
            ]
            v_info_text.set("\n".join(lines))

            s = st.get("settings") or {}
            for k, var in set_vars.items():
                var.set(bool(s.get(k, True if k != "capture_hook" else False)))
            al_var.set(auto_lock_label(int(s.get("auto_lock_minutes", 30))))

            wired = st.get("integrations") or {}
            for target, name in INTEGRATION_TARGETS:
                if target in agent_btns:
                    agent_btns[target].configure(
                        text=f"{name} ✓" if wired.get(target) else name)

        def refresh_summary() -> None:
            def done(out):
                if isinstance(out, Exception):
                    set_status(f"Status error: {out}")
                else:
                    render_summary(out)
            off_ui(ctrl.summary, done)

        def populate_doc_editor(doc: dict | None) -> None:
            app["selected_doc"] = doc
            app["doc_dirty"] = False
            doc_editor.configure(state="normal")
            doc_editor.delete("1.0", "end")
            for t in (out_tree, hist_tree, gmem_tree):
                for item in t.get_children():
                    t.delete(item)
            diff_text.delete("1.0", "end")

            if doc is None:
                d_path_var.set("")
                d_title_var.set("")
                d_ns_var.set("main")
                d_col_var.set("")
                d_tags_var.set("")
                d_stat_var.set("active")
                d_pin_var.set(False)
                d_note_var.set("")
                d_info_var.set("New document - enter a Path and Content, then click Save Version")
                save_doc_btn.configure(state="normal")
                return

            d_path_var.set(doc.get("path") or "")
            d_title_var.set(doc.get("title") or "")
            d_ns_var.set(doc.get("namespace") or "main")
            d_col_var.set(doc.get("collection") or "")
            d_tags_var.set(", ".join(doc.get("tags") or []))
            d_stat_var.set(doc.get("status") or "active")
            d_pin_var.set(bool(doc.get("pinned")))
            d_note_var.set("")

            is_bin = bool(doc.get("is_binary"))
            info = (f"ID {doc['id'][:10]} · v{doc.get('version', 1)} · "
                    f"{doc.get('mime_type', '')} · {_format_size(doc.get('size_bytes', 0))}")
            if doc.get("stale_memory_count"):
                info += f" · {doc['stale_memory_count']} stale memories"
            d_info_var.set(info)

            if is_bin:
                txt = (
                    f"[Binary file: {doc.get('mime_type')}, "
                    f"{_format_size(doc.get('size_bytes', 0))}]\n"
                    "Use 'Export…' to save raw bytes to disk.\n\n"
                    "--- Extracted Searchable Text ---\n"
                    + (doc.get("extracted_text") or "(none)")
                )
                doc_editor.insert("1.0", txt)
                doc_editor.configure(state="disabled")
                save_doc_btn.configure(state="disabled")
            else:
                doc_editor.insert("1.0", doc.get("content") or "")
                save_doc_btn.configure(state="normal")

            for h in (doc.get("outline") or []):
                indent = "  " * max(0, int(h.get("level", 1)) - 1)
                out_tree.insert("", "end", values=(
                    f"{indent}{h.get('heading', '')}", h.get("line", 1)))

            vers = doc.get("versions") or []
            for v_item in vers:
                hist_tree.insert("", "end", iid=str(v_item["version"]), values=(
                    f"v{v_item['version']}",
                    v_item.get("created_local", ""),
                    _format_size(v_item.get("size_bytes", 0)),
                    (v_item.get("sha256") or "")[:12],
                    v_item.get("change_note") or "",
                ))
            if vers:
                cur_v = int(vers[-1]["version"])
                prev_v = int(vers[-2]["version"]) if len(vers) >= 2 else cur_v
                diff_from_var.set(str(prev_v))
                diff_to_var.set(str(cur_v))

            for m in (doc.get("memories") or []):
                st_lbl = "STALE" if m.get("stale") else "OK"
                gmem_tree.insert("", "end", iid=m["id"], values=(
                    st_lbl, f"v{m.get('doc_version', '?')}",
                    m.get("doc_section") or "",
                    " ".join((m.get("text") or "").split()),
                ))

        def open_doc_by_id(did: str, *, version: int | None = None) -> None:
            def done(out):
                if isinstance(out, Exception):
                    set_status(f"Could not open document: {out}")
                    return
                populate_doc_editor(out)
                set_status(f"Opened {out.get('path')} (v{out.get('version')}).")
            off_ui(lambda: ctrl.get_doc(did, version=version), done)

        def load_docs(*, select_id: str | None = None) -> None:
            q = doc_search_var.get().strip()
            status_f = doc_status_var.get()
            pinned_f = bool(doc_pinned_var.get())
            stale_f = bool(doc_stale_var.get())

            def work():
                if q and not stale_f:
                    return ctrl.search_docs(
                        q, status=status_f, pinned_only=pinned_f, top_k=100)
                out = ctrl.list_docs(
                    status=status_f, pinned_only=pinned_f,
                    stale_only=stale_f, limit=500)
                return {"results": out["documents"], "total": out["total"]}

            def done(out):
                if isinstance(out, Exception):
                    set_status(f"Document load failed: {out}")
                    return
                items = out.get("results") or []
                app["docs"] = items
                for iid in doc_tree.get_children():
                    doc_tree.delete(iid)
                for d in items:
                    pin = "★ " if d.get("pinned") else ""
                    mc = int(d.get("memory_count") or 0)
                    sc = int(d.get("stale_memory_count") or 0)
                    m_txt = f"{mc} ({sc}!)" if sc else (str(mc) if mc else "")
                    doc_tree.insert("", "end", iid=d["id"], values=(
                        pin + (d.get("title") or d.get("path") or ""),
                        d.get("path") or "",
                        f"v{d.get('version', 1)}",
                        m_txt,
                    ))
                n = len(items)
                doc_count_var.set(
                    f"{n} {'document' if n == 1 else 'documents'}"
                    + (f" matching {q!r}" if q else ""))
                target = select_id or (
                    app["selected_doc"]["id"]
                    if app["selected_doc"] and app["selected_doc"].get("id") else None)
                if target and doc_tree.exists(target):
                    doc_tree.selection_set(target)
                    doc_tree.see(target)
                    if select_id:
                        open_doc_by_id(select_id)
                elif select_id:
                    open_doc_by_id(select_id)
            off_ui(work, done)

        def on_doc_select(_event=None):
            sel = doc_tree.selection()
            if not sel:
                return
            did = sel[0]
            if app["selected_doc"] and app["selected_doc"].get("id") == did and app["doc_dirty"]:
                return
            if app["doc_dirty"]:
                if not messagebox.askyesno(
                    "Unsaved changes",
                    "Discard unsaved edits in the current document?",
                    parent=root,
                ):
                    return
            open_doc_by_id(did)

        doc_tree.bind("<<TreeviewSelect>>", on_doc_select)

        def start_new_doc():
            if app["doc_dirty"] and not messagebox.askyesno(
                "Unsaved changes", "Discard unsaved edits?", parent=root
            ):
                return
            nb.select(tab_docs)
            doc_tree.selection_remove(doc_tree.selection())
            populate_doc_editor(None)
            d_path_var.set("notes/untitled.md")
            doc_sub_nb.select(sub_content)
            doc_editor.focus_set()

        def do_save_doc():
            if app["selected_doc"] and app["selected_doc"].get("is_binary"):
                messagebox.showinfo(
                    "Binary document",
                    "Binary documents cannot be edited as text. Import a new "
                    "file version or export it to disk.", parent=root)
                return
            path = d_path_var.get().strip()
            if not path:
                messagebox.showwarning("Missing path",
                                       "Enter a document path (e.g. notes/spec.md).",
                                       parent=root)
                return
            content = doc_editor.get("1.0", "end-1c")
            did = app["selected_doc"]["id"] if app["selected_doc"] else None

            def work():
                return ctrl.save_doc(
                    path=path,
                    content=content,
                    title=d_title_var.get().strip() or None,
                    namespace=d_ns_var.get().strip() or "main",
                    collection=d_col_var.get().strip() or None,
                    tags=_parse_tags(d_tags_var.get()),
                    status=d_stat_var.get() or "active",
                    pinned=bool(d_pin_var.get()),
                    change_note=d_note_var.get().strip() or None,
                    doc_id=did,
                )

            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Save failed", str(out), parent=root)
                    return
                app["doc_dirty"] = False
                stale = out.get("stale_memories") or []
                note = f"Saved {out['path']} v{out['version']}."
                if stale:
                    note += f" ({len(stale)} grounded memories flagged stale)"
                set_status(note)
                load_docs(select_id=out["id"])
                refresh_summary()
            off_ui(work, done, busy_msg=f"Saving {path}…")

        def open_patch_dialog():
            doc = app["selected_doc"]
            if not doc or doc.get("is_binary"):
                messagebox.showinfo("Patch document",
                                    "Select a saved text document to patch.",
                                    parent=root)
                return
            dlg = tk.Toplevel(root)
            dlg.title(f"Patch {doc['path']}")
            dlg.geometry(f"{int(520 * S)}x{int(340 * S)}")
            frm = ttk.Frame(dlg, padding=12)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="Section heading (optional, leave blank to append):").pack(anchor="w")
            sec_v = tk.StringVar()
            ttk.Entry(frm, textvariable=sec_v).pack(fill="x", pady=(2, 8))
            ttk.Label(frm, text="Change note:").pack(anchor="w")
            cn_v = tk.StringVar()
            ttk.Entry(frm, textvariable=cn_v).pack(fill="x", pady=(2, 8))
            ttk.Label(frm, text="Text to append or set as section content:").pack(anchor="w")
            txt = scrolledtext.ScrolledText(frm, height=8, font=mono_font)
            txt.pack(fill="both", expand=True, pady=(2, 8))

            def apply_patch():
                body = txt.get("1.0", "end-1c")
                sec = sec_v.get().strip() or None
                cn = cn_v.get().strip() or None
                dlg.destroy()

                def work():
                    if sec:
                        return ctrl.patch_doc(
                            doc["id"], section=sec, section_content=body,
                            change_note=cn)
                    return ctrl.patch_doc(
                        doc["id"], append=body, change_note=cn)

                def done(out):
                    if isinstance(out, Exception):
                        messagebox.showerror("Patch failed", str(out), parent=root)
                        return
                    set_status(f"Patched {out['path']} -> v{out['version']}.")
                    load_docs(select_id=out["id"])
                    refresh_summary()
                off_ui(work, done, busy_msg="Patching document…")

            bar = ttk.Frame(frm)
            bar.pack(fill="x")
            ttk.Button(bar, text="Apply Patch", command=apply_patch).pack(side="left")
            ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)

        def do_import_file():
            fp = filedialog.askopenfilename(
                parent=root, title="Import File into Encrypted Vault")
            if not fp:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Import failed", str(out), parent=root)
                    return
                did = (out.get("files") or [{}])[0].get("id")
                set_status(f"Imported {Path(fp).name}.")
                nb.select(tab_docs)
                load_docs(select_id=did)
                refresh_summary()
            off_ui(lambda: ctrl.import_path(fp), done,
                   busy_msg=f"Encrypting and importing {Path(fp).name}…")

        def do_import_folder():
            folder = filedialog.askdirectory(
                parent=root, title="Import Folder Tree into Encrypted Vault")
            if not folder:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Import failed", str(out), parent=root)
                    return
                set_status(
                    f"Folder import: +{out['imported']} new, "
                    f"{out['updated']} updated, {out['unchanged']} unchanged, "
                    f"{out['skipped']} skipped.")
                load_docs()
                refresh_summary()
            off_ui(lambda: ctrl.import_path(folder), done,
                   busy_msg=f"Importing folder {Path(folder).name}…")

        def do_export_doc():
            doc = app["selected_doc"]
            if not doc:
                messagebox.showinfo("Export", "Select a document first.", parent=root)
                return
            dest = filedialog.asksaveasfilename(
                parent=root, title="Export Document to Disk",
                initialfile=Path(doc["path"]).name)
            if not dest:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Export failed", str(out), parent=root)
                    return
                set_status(f"Exported {doc['path']} -> {out['dest']}.")
            off_ui(lambda: ctrl.export_doc(doc["id"], dest), done,
                   busy_msg="Exporting document…")

        def do_archive_doc():
            doc = app["selected_doc"]
            if not doc:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Archive failed", str(out), parent=root)
                    return
                set_status(f"Archived {doc['path']}.")
                populate_doc_editor(None)
                load_docs()
                refresh_summary()
            off_ui(lambda: ctrl.archive_doc(doc["id"]), done)

        def do_delete_doc():
            doc = app["selected_doc"]
            if not doc:
                return
            shred = messagebox.askyesnocancel(
                "Delete Document",
                f"Permanently delete {doc['path']}?\n\n"
                "Yes = Crypto-shred (unrecoverable, rewrites vault)\n"
                "No  = Delete normally\n"
                "Cancel = Keep document",
                parent=root,
            )
            if shred is None:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Delete failed", str(out), parent=root)
                    return
                aff = out.get("affected_memory_ids") or []
                note = (f"Crypto-shredded {doc['path']}." if shred
                        else f"Deleted {doc['path']}.")
                if aff:
                    note += f" ({len(aff)} grounded memories retained as stale)"
                set_status(note)
                populate_doc_editor(None)
                load_docs()
                refresh_summary()
            off_ui(lambda: ctrl.delete_doc(doc["id"], shred=bool(shred)), done,
                   busy_msg="Deleting document…")

        def do_show_diff():
            doc = app["selected_doc"]
            if not doc:
                return
            try:
                v1, v2 = int(diff_from_var.get()), int(diff_to_var.get())
            except ValueError:
                messagebox.showwarning("Versions", "Enter integer version numbers.",
                                       parent=root)
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Diff failed", str(out), parent=root)
                    return
                diff_text.delete("1.0", "end")
                diff_text.insert("1.0", out.get("diff") or "(no textual differences)")
            off_ui(lambda: ctrl.diff_doc(doc["id"], v1, v2), done)

        def do_load_selected_version():
            doc = app["selected_doc"]
            sel = hist_tree.selection()
            if not doc or not sel:
                return
            open_doc_by_id(doc["id"], version=int(sel[0]))

        def do_shred_selected_version():
            doc = app["selected_doc"]
            sel = hist_tree.selection()
            if not doc or not sel:
                return
            ver = int(sel[0])
            if not messagebox.askyesno(
                "Crypto-Shred Version",
                f"Permanently crypto-shred version v{ver} of {doc['path']}?",
                parent=root,
            ):
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Shred failed", str(out), parent=root)
                    return
                set_status(f"Crypto-shredded {doc['path']} v{ver}.")
                open_doc_by_id(doc["id"])
                refresh_summary()
            off_ui(lambda: ctrl.delete_doc(doc["id"], version=ver, shred=True),
                   done, busy_msg=f"Crypto-shredding v{ver}…")

        def do_store_grounded_memory():
            doc = app["selected_doc"]
            if not doc:
                messagebox.showinfo("Ground memory",
                                    "Save or select a document first.", parent=root)
                return
            claim = g_claim_var.get().strip()
            if not claim:
                return
            sec = g_sec_var.get().strip() or None
            kind = g_kind_var.get() or "fact"

            def work():
                return ctrl.store_memory(
                    claim, source=f"doc:{doc['path']}", kind=kind,
                    namespace=doc.get("namespace") or "main",
                    doc_id=doc["id"], doc_section=sec, keep_both=True)

            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Store failed", str(out), parent=root)
                    return
                g_claim_var.set("")
                set_status(f"Stored grounded memory linked to {doc['path']} v{doc['version']}.")
                open_doc_by_id(doc["id"])
                load_docs()
                refresh_summary()
            off_ui(work, done, busy_msg="Storing grounded memory…")

        def do_forget_grounded_memory():
            doc = app["selected_doc"]
            sel = gmem_tree.selection()
            if not doc or not sel:
                return
            rid = sel[0]
            if not messagebox.askyesno("Forget memory", "Delete this memory?",
                                       parent=root):
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Forget failed", str(out), parent=root)
                    return
                set_status("Deleted memory.")
                open_doc_by_id(doc["id"])
                refresh_summary()
            off_ui(lambda: ctrl.forget_memory(rid), done)

        def open_compile_dialog():
            dlg = tk.Toplevel(root)
            dlg.title("Compile Memories into Document")
            dlg.geometry(f"{int(440 * S)}x{int(260 * S)}")
            frm = ttk.Frame(dlg, padding=12)
            frm.pack(fill="both", expand=True)
            p_v = tk.StringVar(value="compiled/summary.md")
            t_v = tk.StringVar(value="Compiled Notes")
            q_v = tk.StringVar()
            tg_v = tk.StringVar()
            for lbl, var in (
                ("Document path:", p_v), ("Title:", t_v),
                ("Search query (optional):", q_v), ("Tags filter (comma-separated):", tg_v),
            ):
                ttk.Label(frm, text=lbl).pack(anchor="w")
                ttk.Entry(frm, textvariable=var).pack(fill="x", pady=(2, 6))

            def run_compile():
                path = p_v.get().strip()
                if not path:
                    return
                dlg.destroy()
                def done(out):
                    if isinstance(out, Exception):
                        messagebox.showerror("Compile failed", str(out), parent=root)
                        return
                    set_status(
                        f"Compiled {out.get('compiled_memories', 0)} memories -> {out['path']}.")
                    load_docs(select_id=out["id"])
                    refresh_summary()
                off_ui(
                    lambda: ctrl.compile_memories(
                        path, title=t_v.get().strip() or None,
                        query=q_v.get().strip() or None,
                        tags=_parse_tags(tg_v.get()) or None),
                    done, busy_msg="Compiling memories into document…")

            bar = ttk.Frame(frm)
            bar.pack(fill="x", pady=(6, 0))
            ttk.Button(bar, text="Compile", command=run_compile).pack(side="left")
            ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)

        # ---- Memories handlers ----

        def load_memories(mode: str = "recent") -> None:
            q = mem_q_var.get().strip()
            def work():
                if mode == "stale":
                    out = ctrl.stale_memories()
                    items = out.get("stale") or []
                    for it in items:
                        it.setdefault("doc_stale", True)
                    return items
                if mode == "search" and q:
                    out = ctrl.search_memories(q, top_k=50, include_documents=False)
                    return out.get("results") or []
                out = ctrl.recent_memories(limit=80, include_seeded=False)
                return out.get("results") or []

            def done(out):
                if isinstance(out, Exception):
                    set_status(f"Memory load failed: {out}")
                    return
                app["memories"] = out
                for iid in mem_tree.get_children():
                    mem_tree.delete(iid)
                for m in out:
                    doc_s = ""
                    if m.get("doc_path"):
                        doc_s = f"{m['doc_path']} v{m.get('doc_version', '?')}"
                        if m.get("doc_stale") or m.get("stale"):
                            doc_s += " [STALE]"
                    sc_s = f"{m['score']:.3f}" if "score" in m else ""
                    mem_tree.insert("", "end", iid=m["id"], values=(
                        m.get("kind") or "fact",
                        sc_s,
                        m.get("created_local") or "",
                        doc_s,
                        " ".join((m.get("text") or "").split()),
                    ))
                set_status(f"Loaded {len(out)} memories ({mode}).")
            off_ui(work, done)

        def on_mem_select(_event=None):
            sel = mem_tree.selection()
            if not sel:
                m_open_doc_btn.configure(state="disabled")
                return
            rid = sel[0]
            m = next((x for x in app["memories"] if x["id"] == rid), None)
            if m is None:
                return
            lines = [
                f"ID:        {m['id']}",
                f"Kind:      {m.get('kind', 'fact')}   Namespace: {m.get('namespace', 'main')}",
                f"Created:   {m.get('created_local', '')}",
                f"Source:    {m.get('source') or '-'}",
                f"Tags:      {', '.join(m.get('tags') or []) or '-'}",
            ]
            if m.get("expires"):
                lines.append(f"Expires:   {m['expires']}")
            if m.get("doc_id"):
                st = " [STALE]" if (m.get("doc_stale") or m.get("stale")) else ""
                lines.append(
                    f"Document:  {m.get('doc_path') or m['doc_id']} "
                    f"v{m.get('doc_version', '?')}{st}")
                m_open_doc_btn.configure(state="normal")
            else:
                m_open_doc_btn.configure(state="disabled")
            lines.extend(["", m.get("text") or ""])
            mem_detail.configure(state="normal")
            mem_detail.delete("1.0", "end")
            mem_detail.insert("1.0", "\n".join(lines))
            mem_detail.configure(state="disabled")

        mem_tree.bind("<<TreeviewSelect>>", on_mem_select)

        def open_selected_memory_doc():
            sel = mem_tree.selection()
            if not sel:
                return
            m = next((x for x in app["memories"] if x["id"] == sel[0]), None)
            if m and m.get("doc_id"):
                nb.select(tab_docs)
                load_docs(select_id=m["doc_id"])

        def do_forget_selected_memory(*, shred: bool):
            sel = mem_tree.selection()
            if not sel:
                return
            rid = sel[0]
            verb = "Permanently crypto-shred" if shred else "Delete"
            if not messagebox.askyesno("Confirm", f"{verb} this memory?",
                                       parent=root):
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Error", str(out), parent=root)
                    return
                set_status("Crypto-shredded memory." if shred else "Deleted memory.")
                load_memories("recent")
                refresh_summary()
            off_ui(lambda: ctrl.forget_memory(rid, shred=shred), done)

        def open_add_memory_dialog():
            dlg = tk.Toplevel(root)
            dlg.title("Add Atomic Memory")
            dlg.geometry(f"{int(500 * S)}x{int(340 * S)}")
            frm = ttk.Frame(dlg, padding=12)
            frm.pack(fill="both", expand=True)

            claim_v = tk.StringVar()
            cnt_v = tk.StringVar(value=f"0/{MAX_CHARS}")
            claim_v.trace_add(
                "write", lambda *_: cnt_v.set(f"{len(claim_v.get())}/{MAX_CHARS}"))
            src_v = tk.StringVar(value="from gui")
            kind_v = tk.StringVar(value="fact")
            ns_v = tk.StringVar(value="main")
            tags_v = tk.StringVar()
            exp_v = tk.StringVar()
            cur_doc = app["selected_doc"]
            doc_v = tk.StringVar(value=(cur_doc["id"] if cur_doc else ""))

            hdr = ttk.Frame(frm)
            hdr.pack(fill="x")
            ttk.Label(hdr, text="One plain-prose claim (<= 200 chars):").pack(side="left")
            ttk.Label(hdr, textvariable=cnt_v, font=small_font).pack(side="right")
            ttk.Entry(frm, textvariable=claim_v).pack(fill="x", pady=(2, 8))

            for lbl, var in (
                ("Source (required):", src_v),
                ("Namespace:", ns_v),
                ("Tags (comma-separated):", tags_v),
                ("Expires (optional, e.g. 2027-01-01 or 3m):", exp_v),
                ("Source Document ID (optional):", doc_v),
            ):
                ttk.Label(frm, text=lbl).pack(anchor="w")
                ttk.Entry(frm, textvariable=var).pack(fill="x", pady=(1, 5))

            kr = ttk.Frame(frm)
            kr.pack(fill="x", pady=(2, 8))
            ttk.Label(kr, text="Kind:").pack(side="left")
            ttk.Combobox(kr, textvariable=kind_v, values=("fact", "opinion"),
                         state="readonly", width=10).pack(side="left", padx=(6, 0))

            def submit_mem():
                txt = claim_v.get().strip()
                if not txt:
                    return
                def done(out):
                    if isinstance(out, Exception):
                        messagebox.showerror("Store failed", str(out), parent=dlg)
                        return
                    dlg.destroy()
                    set_status("Memory stored.")
                    load_memories("recent")
                    refresh_summary()
                off_ui(
                    lambda: ctrl.store_memory(
                        txt, source=src_v.get().strip() or "from gui",
                        kind=kind_v.get(), namespace=ns_v.get().strip() or "main",
                        tags=_parse_tags(tags_v.get()) or None,
                        expires=exp_v.get().strip() or None,
                        doc_id=doc_v.get().strip() or None,
                        keep_both=True),
                    done)

            bar = ttk.Frame(frm)
            bar.pack(fill="x")
            ttk.Button(bar, text="Store", command=submit_mem).pack(side="left")
            ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)

        # ---- Relations handlers ----

        def load_relations() -> None:
            ent = rel_ent_var.get().strip() or None
            def done(out):
                if isinstance(out, Exception):
                    set_status(f"Relations error: {out}")
                    return
                rels = out.get("relations") or []
                app["relations"] = rels
                for iid in rel_tree.get_children():
                    rel_tree.delete(iid)
                for r in rels:
                    rel_tree.insert("", "end", iid=r["id"], values=(
                        r.get("subject", ""), r.get("predicate", ""),
                        r.get("object", ""), r.get("namespace", ""),
                        (r.get("doc_id") or "")[:10],
                    ))
            off_ui(lambda: ctrl.list_relations(entity=ent), done)

        def do_add_relation():
            s, p, o = rs_var.get().strip(), rp_var.get().strip(), ro_var.get().strip()
            if not (s and p and o):
                return
            ns = rns_var.get().strip() or "main"
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Link failed", str(out), parent=root)
                    return
                rs_var.set("")
                rp_var.set("")
                ro_var.set("")
                set_status("Relation added.")
                load_relations()
                refresh_summary()
            off_ui(lambda: ctrl.add_relation(s, p, o, namespace=ns), done)

        def do_unlink_relation():
            sel = rel_tree.selection()
            if not sel:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Unlink failed", str(out), parent=root)
                    return
                set_status("Relation removed.")
                load_relations()
                refresh_summary()
            off_ui(lambda: ctrl.unlink_relation(sel[0]), done)

        # ---- Vault maintenance handlers ----

        def do_merge_vault():
            src = filedialog.askopenfilename(
                parent=root, title="Merge Encrypted Vault Backup",
                filetypes=[("Encrypted Compartment Vault", "*.vault"),
                           ("All files", "*.*")])
            if not src:
                return
            def handle(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Merge failed", str(out), parent=root)
                    return
                ok, msg, needs_pw = out
                if needs_pw:
                    pw_dlg = tk.Toplevel(root)
                    pw_dlg.title("Backup Vault Passphrase")
                    pf = ttk.Frame(pw_dlg, padding=12)
                    pf.pack(fill="both", expand=True)
                    ttk.Label(pf, text=f"Enter passphrase for {Path(src).name}:").pack(anchor="w")
                    pe = ttk.Entry(pf, show="\u2022", width=32)
                    pe.pack(fill="x", pady=6)
                    pe.focus_set()
                    def go(*_):
                        pw = pe.get()
                        pe.delete(0, "end")
                        pw_dlg.destroy()
                        off_ui(lambda: ctrl.merge(src, pw), handle,
                               busy_msg=f"Merging {Path(src).name}…")
                    pe.bind("<Return>", go)
                    ttk.Button(pf, text="Merge", command=go).pack(side="left")
                    ttk.Button(pf, text="Cancel", command=pw_dlg.destroy).pack(side="left", padx=6)
                    return
                set_status(msg)
                if ok:
                    refresh_all(force=True)
                else:
                    messagebox.showerror("Merge failed", msg, parent=root)
            off_ui(lambda: ctrl.merge(src), handle, busy_msg=f"Merging {Path(src).name}…")

        def open_change_pw_dialog():
            dlg = tk.Toplevel(root)
            dlg.title("Change Vault Passphrase")
            frm = ttk.Frame(dlg, padding=14)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="New passphrase:").pack(anchor="w")
            p1 = ttk.Entry(frm, show="\u2022", width=34)
            p1.pack(fill="x", pady=(2, 8))
            ttk.Label(frm, text="Repeat new passphrase:").pack(anchor="w")
            p2 = ttk.Entry(frm, show="\u2022", width=34)
            p2.pack(fill="x", pady=(2, 8))
            p1.focus_set()

            def go(*_):
                a, b = p1.get(), p2.get()
                p1.delete(0, "end")
                p2.delete(0, "end")
                def done(out):
                    ok, msg = (False, str(out)) if isinstance(out, Exception) else out
                    if ok:
                        dlg.destroy()
                        set_status(msg)
                    else:
                        messagebox.showerror("Change password", msg, parent=dlg)
                off_ui(lambda: ctrl.change_passphrase(a, b), done,
                       busy_msg="Updating vault passphrase…")

            p2.bind("<Return>", go)
            bar = ttk.Frame(frm)
            bar.pack(fill="x", pady=(4, 0))
            ttk.Button(bar, text="Save", command=go).pack(side="left")
            ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)

        def do_export_all():
            dest = filedialog.askdirectory(
                parent=root, title="Export All Documents to Folder")
            if not dest:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Export failed", str(out), parent=root)
                    return
                set_status(f"Exported {out['exported']} documents -> {out['dest']}.")
            off_ui(lambda: ctrl.export_all(dest), done,
                   busy_msg="Exporting all documents…")

        def do_export_jsonl():
            dest = filedialog.asksaveasfilename(
                parent=root, title="Export Plaintext JSONL (with documents)",
                initialfile="compartment-export.jsonl",
                defaultextension=".jsonl",
                filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")])
            if not dest:
                return
            if not messagebox.askyesno(
                "Plaintext Export",
                "JSONL export writes decrypted memories and documents to disk.\n"
                "Continue?", parent=root,
            ):
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Export failed", str(out), parent=root)
                    return
                set_status(f"Exported {out['lines']} JSONL records -> {out['dest']}.")
            off_ui(lambda: ctrl.export_jsonl(dest), done, busy_msg="Exporting JSONL…")

        def do_import_jsonl():
            src = filedialog.askopenfilename(
                parent=root, title="Import JSONL into Encrypted Vault",
                filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")])
            if not src:
                return
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Import failed", str(out), parent=root)
                    return
                set_status(f"Imported {out['imported']} records from JSONL.")
                refresh_all(force=True)
            off_ui(lambda: ctrl.import_jsonl(src), done, busy_msg="Importing JSONL…")

        def do_verify_audit():
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Audit verify", str(out), parent=root)
                    return
                msg = f"Audit chain {'OK' if out['ok'] else 'FAILED'} ({out['entries']} entries): {out['detail']}"
                set_status(msg)
                messagebox.showinfo("Audit Chain Verification", msg, parent=root)
            off_ui(ctrl.verify_audit, done, busy_msg="Verifying hash-chained audit log…")

        def do_selftest():
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Selftest failed", str(out), parent=root)
                    return
                failed = int(out.get("failed") or 0)
                passed = int(out.get("passed") or 0)
                msg = f"Selftest {'PASSED' if failed == 0 else 'FAILED'} ({passed}/{passed + failed})"
                set_status(msg)
                messagebox.showinfo("Vault Selftest", json.dumps(out, indent=2),
                                    parent=root)
            off_ui(ctrl.run_selftest, done, busy_msg="Running offline selftest…")

        def do_expire_now():
            def done(out):
                if isinstance(out, Exception):
                    messagebox.showerror("Expire failed", str(out), parent=root)
                    return
                set_status(f"Removed {out['expired']} expired memories.")
                refresh_all(force=True)
            off_ui(ctrl.expire_now, done, busy_msg="Sweeping expired memories…")

        def do_lock():
            if app["doc_dirty"] and not messagebox.askyesno(
                "Unsaved edits", "Lock vault and discard unsaved editor changes?",
                parent=root,
            ):
                return
            def done(_out):
                show_gate("Vault locked.")
                set_status("Vault locked; key cleared from RAM.")
            off_ui(ctrl.lock, done, busy_msg="Locking vault…")

        def refresh_all(*, force: bool = False):
            def sync_work():
                if not ctrl.check_session_alive():
                    return "locked"
                ctrl.sync_if_changed(force=force)
                return "ok"
            def sync_done(out):
                if out == "locked":
                    show_gate("Vault was locked externally.")
                    set_status("Vault locked.")
                    return
                refresh_summary()
                load_docs(select_id=select_doc_id if not app["docs"] else None)
                load_memories("recent")
                load_relations()
            off_ui(sync_work, sync_done)

        # ---- Keyboard shortcuts ----
        mod = "Command" if sys.platform == "darwin" else "Control"
        root.bind_all(f"<{mod}-s>", lambda *_: (do_save_doc(), "break")[1])
        root.bind_all(f"<{mod}-n>", lambda *_: (start_new_doc(), "break")[1])
        root.bind_all(f"<{mod}-r>", lambda *_: (refresh_all(force=True), "break")[1])
        root.bind_all(f"<{mod}-l>", lambda *_: (do_lock(), "break")[1])

        def _focus_search(*_):
            cur = nb.select()
            if cur == str(tab_mems):
                mem_q_entry.focus_set()
                mem_q_entry.select_range(0, "end")
            else:
                nb.select(tab_docs)
                doc_search_entry.focus_set()
                doc_search_entry.select_range(0, "end")
            return "break"

        root.bind_all(f"<{mod}-f>", _focus_search)

        # Initial load
        refresh_summary()
        load_docs(select_id=select_doc_id)
        load_memories("recent")
        load_relations()

    # ---- periodic external-lock & agent-write watcher ----

    def watch_tick():
        if app["closing"]:
            return
        if ctrl.is_open:
            def work():
                if not ctrl.check_session_alive():
                    return "locked"
                changed = ctrl.sync_if_changed()
                return "changed" if changed else "same"
            def done(out):
                if out == "locked":
                    show_gate("Vault was locked from another window or terminal.")
                    set_status("Vault locked; key dropped.")
                elif out == "changed":
                    set_status("Vault updated by another process - click Refresh to reload.")
            off_ui(work, done)
        app["watch_id"] = root.after(WATCH_INTERVAL_MS, watch_tick)

    def on_close():
        app["closing"] = True
        if app.get("watch_id") is not None:
            with contextlib.suppress(Exception):
                root.after_cancel(app["watch_id"])
        ctrl.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # Open straight into workspace if already unlocked, otherwise show Lock/Create.
    opened, msg = ctrl.try_open_from_session()
    if opened:
        show_workspace(select_doc_id=initial_doc_id)
        set_status("Vault unlocked and ready.")
    else:
        show_gate("" if not ctrl.exists else "")
        set_status(msg)

    app["watch_id"] = root.after(WATCH_INTERVAL_MS, watch_tick)
    return app


def run(vault: str | None = None, *, doc_id: str | None = None,
        check: bool = False) -> int:
    """Launch Compartment's optional cross-platform desktop GUI."""
    if check:
        return self_check(vault)

    from . import systray

    try:
        import tkinter as tk
    except ImportError:
        print(systray.tk_missing_message("GUI"), file=sys.stderr)
        return 3

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(systray.no_display_message(exc), file=sys.stderr)
        return 4

    ctrl = GuiController(vault)
    try:
        _build_window(root, ctrl, initial_doc_id=doc_id)
        root.mainloop()
    finally:
        ctrl.close()
    return 0


__all__ = ["GuiController", "run", "self_check"]
