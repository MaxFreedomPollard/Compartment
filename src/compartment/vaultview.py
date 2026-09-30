"""The vault, held open for the status panel, and kept current by reading
only what changed.

The menu bar and the tray show four things: whether the vault is open, how
many memories it holds, how many of those were stored during use, and the
last few. Every click used to start two fresh `compartment` processes. Each
one decrypted the whole vault - thousands of starting memories - to report a
count and five lines, then threw that work away, and the second one resealed
and rewrote the vault file on its way out, so a glance at the panel made the
copy in every running agent stale.

This module runs as ONE long-lived child of the panel, talking JSON lines over
its own stdin and stdout: no socket, no port, nothing any other process can
reach. It opens the vault once, and after that:

    nothing changed on disk        -> answers from memory; one stat
    agents appended memories       -> reads and decrypts only those journal
                                      entries, typically a few hundred bytes
    something rewrote the file     -> reads it again; the one case where a
                                      full decrypt cannot be avoided

It never writes. The ordinary way of opening a vault (Vault.unlock) compacts a
replayed journal, sweeps expired memories and appends audit rows; this does
none of them. Expired memories are left out of the numbers instead of being
deleted, so the counts match what `compartment recent` would report.

Locked means what it means everywhere else: no stored credential that opens
this vault (Vault.find_credential). A read that failed or took too long is
reported as an error, never as "locked", and the panel keeps showing what it
last knew. Only `compartment lock`, `compartment unlock`, the panel's own
buttons, or a restart change what it shows.

Key material. While it holds the vault this process holds the master key, as
any `compartment serve` does. It lets go by exiting after IDLE_SECONDS without
a request, and drops the key within CREDENTIAL_CHECK_SECONDS of the stored
credential disappearing (a `compartment lock` typed in a terminal). The panel
starts it again the next time it needs it.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

from . import crypto, session, vaultfile
from .acl import VaultConfig
from .crypto import CryptoError, TamperError
from .platforms import file_signature
from .store import Store
from .vault import Vault

#: How long the process stays after its last request. Long enough that a
#: burst of clicks costs one open; short enough that the key and the
#: decrypted vault do not sit in memory all day for a panel opened twice.
#: Shorter than every auto-lock choice the panel offers (15 minutes and up).
IDLE_SECONDS = 600

#: How often a process holding the key looks for its credential, so that a
#: `compartment lock` typed in a terminal takes the key out of this process
#: too, and not only at the next click.
CREDENTIAL_CHECK_SECONDS = 30

#: The CLI's default caller, so the panel shows the namespaces, and therefore
#: the numbers, that `compartment recent` shows.
CALLER = "user"

RECENT_DEFAULT = 5


class CredentialRefused(CryptoError):
    """A stored credential exists but does not open this vault."""


class VaultView:
    """The vault as the panel needs it: rows only, no search index, read-only.

    Not thread-safe; the loop below makes one call at a time."""

    def __init__(self, path: str, caller: str = CALLER):
        self.path = path
        self.caller = caller
        #: (passphrase, keyslots, master key) from the last passphrase
        #: unwrap, so a passphrase given in the environment costs one Argon2
        #: run rather than one per reload. Only reused for identical slots,
        #: and forgotten by drop() along with everything else.
        self._unwrapped: tuple[str, str, bytes] | None = None
        self._reset()

    # ---------------------------------------------------------------- state

    @property
    def holding(self) -> bool:
        return self._v is not None

    def drop(self) -> None:
        """Let go of the vault and its key - every copy of it, the one
        derived from a passphrase included."""
        self._reset()
        self._unwrapped = None

    def _reset(self) -> None:
        """Forget the open vault. A reload starts here, and keeps the
        passphrase unwrap so that it does not run Argon2 again."""
        self._v: Vault | None = None
        self._key: bytes | None = None
        self._ino = None
        self._identity = b""      # header + payload nonce, as last read
        self._end = 0             # end of the last complete journal entry
        self._seq = 0             # journal entries replayed so far
        self._cred_sig = None
        self._cfg_sig = None

    # ---------------------------------------------------------------- reads

    def _master(self, loaded, pw: str | None, key: bytes | None) -> bytes:
        if key is not None:
            return key
        slots = crypto.canonical_json(loaded.header.keyslots).decode()
        if self._unwrapped and self._unwrapped[:2] == (pw, slots):
            return self._unwrapped[2]
        try:
            master = crypto.unwrap_master(
                loaded.header.keyslots, pw,
                keyfile=Vault.load_keyfile_hint(self.path))
        except CryptoError as exc:
            raise CredentialRefused(str(exc)) from exc
        self._unwrapped = (pw, slots, master)
        return master

    def _load(self, pw: str | None, key: bytes | None) -> None:
        """Read and decrypt the whole file. Writes nothing."""
        self._reset()
        with open(self.path, "rb") as f:
            ino = os.fstat(f.fileno()).st_ino
            raw = f.read()
        loaded = vaultfile.parse_vault_bytes(raw, self.path)
        master = self._master(loaded, pw, key)
        try:
            sections = vaultfile.decrypt_payload(loaded.header,
                                                 loaded.payload_ct, master)
        except TamperError as exc:
            # The payload is sealed under the master key, so a key that fails
            # here is a credential for some other vault: the one at this path
            # was replaced, or the passphrase in the environment is stale.
            raise CredentialRefused(str(exc)) from exc
        cfg_sig = file_signature(VaultConfig.path_for(self.path))
        v = Vault(self.path, loaded.header, Store(sections["sqlite"]), master,
                  VaultConfig.load(self.path), build_index=False)
        for entry in vaultfile.decrypt_journal(loaded.header,
                                               loaded.journal_cts, master):
            v._replay(entry)
        header_end = loaded.journal_start - loaded.header.payload_len
        self._v, self._key, self._ino = v, master, ino
        # Every save reseals the payload under a fresh random nonce, which
        # sits at the front of the payload. So header + nonce identify one
        # particular write of the file, even where a filesystem hands a new
        # file the inode number an old one had.
        self._identity = raw[:header_end + crypto.NONCE_LEN]
        self._end = loaded.journal_end
        self._seq = len(loaded.journal_cts)
        self._cred_sig = session.watch_signature(self.path)
        self._cfg_sig = cfg_sig

    def _catch_up(self) -> str:
        """Bring the open vault up to date. Returns what that took:
        "cached" (nothing changed), "tail" (only new journal entries were
        read) or "full" (the file was rewritten, so it was read again)."""
        with open(self.path, "rb") as f:
            st = os.fstat(f.fileno())
            rewritten = st.st_ino != self._ino or st.st_size < self._end
            if not rewritten:
                rewritten = f.read(len(self._identity)) != self._identity
            if not rewritten:
                if st.st_size == self._end:
                    return "cached"
                f.seek(self._end)
                tail = f.read()
        if rewritten:
            return "full"
        cts, used, _partial, _bytes = vaultfile.parse_journal(tail, 0)
        if cts:
            # All or nothing: an entry that fails part way leaves the rows in
            # RAM half-updated, and the caller answers that with a full read.
            for entry in vaultfile.decrypt_journal(self._v.header, cts,
                                                   self._key,
                                                   first_seq=self._seq):
                self._v._replay(entry)
            self._seq += len(cts)
            self._end += used
        # A partial entry after the last whole one is an append still being
        # written. It is read next time, once it is complete.
        return "tail" if cts else "cached"

    def _config_current(self) -> None:
        """Settings the numbers depend on - whether expired memories count,
        which namespaces are readable - are re-read when their file changes."""
        sig = file_signature(VaultConfig.path_for(self.path))
        if sig != self._cfg_sig:
            self._v.config = VaultConfig.load(self.path)
            self._cfg_sig = sig

    # ---------------------------------------------------------------- answers

    def snapshot(self, limit: int = RECENT_DEFAULT) -> dict:
        """Everything the panel shows about the vault itself. Never raises."""
        try:
            os.stat(self.path)
        except FileNotFoundError:
            self.drop()
            return {"ok": True, "exists": False}
        except OSError as exc:
            # A folder it may not read, a network share that stalled: the
            # vault may well be there. Not the same as missing.
            return {"ok": False, "exists": True,
                    "error": f"could not look at the vault file: {exc}"}
        try:
            found = Vault.find_credential(self.path)
        except CryptoError as exc:
            # Could not tell: an unreadable credential file, a boot that
            # cannot be identified. Not the same thing as locked.
            return {"ok": False, "exists": True,
                    "error": f"could not check the stored unlock: {exc}"}
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "exists": True,
                    "error": f"{type(exc).__name__}: {exc}"}
        if found is None:
            self.drop()
            return {"ok": True, "exists": True, "locked": True}
        pw, key = found
        if self._v is not None and key is not None and key != self._key:
            self.drop()               # a different credential: read afresh
        try:
            if self._v is None:
                self._load(pw, key)
                how = "full"
            else:
                try:
                    how = self._catch_up()
                except Exception:                       # noqa: BLE001
                    how = "full"      # anything odd in the tail: start over
                if how == "full":
                    self._load(pw, key)
            self._config_current()
            out = self._v.peek_recent(self.caller, limit=limit)
        except CredentialRefused:
            self.drop()
            return {"ok": True, "exists": True, "locked": True,
                    "credential_refused": True}
        except Exception as exc:                        # noqa: BLE001
            self.drop()
            return {"ok": False, "exists": True,
                    "error": f"{type(exc).__name__}: {exc}"}
        counts = out["counts"]
        return {"ok": True, "exists": True, "locked": False,
                "records": counts["total"], "organic": counts["organic"],
                "recent": out["results"], "read": how}

    def check_credential(self) -> None:
        """Between requests: drop the key if its credential has gone."""
        if self._v is None:
            return
        sig = session.watch_signature(self.path)
        if sig == self._cred_sig:
            return
        try:
            found = Vault.find_credential(self.path)
        except Exception:                               # noqa: BLE001
            return            # could not tell; the next request asks again
        if found is None or (found[1] is not None and found[1] != self._key):
            self.drop()
        else:
            self._cred_sig = sig


# ------------------------------------------------------------------ process

def _handle(view: VaultView, line: str) -> dict:
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("not an object")
    except ValueError:
        return {"ok": False, "error": "request is not a JSON object"}
    op = req.get("op")
    if op == "snapshot":
        try:
            limit = int(req.get("limit", RECENT_DEFAULT))
        except (TypeError, ValueError):
            limit = RECENT_DEFAULT
        out = view.snapshot(limit=limit)
    elif op == "drop":
        view.drop()
        out = {"ok": True}
    elif op == "ping":
        out = {"ok": True, "pid": os.getpid(), "holding": view.holding}
    else:
        out = {"ok": False, "error": f"unknown op {op!r}"}
    out["id"] = req.get("id")
    return out


def serve(path: str, stdin=None, out=None, idle: float = IDLE_SECONDS,
          check_every: float = CREDENTIAL_CHECK_SECONDS) -> int:
    """Answer requests, one JSON object per line, until stdin closes (the
    panel quit or died) or nothing has been asked for `idle` seconds."""
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    view = VaultView(path)
    lines: queue.Queue = queue.Queue()

    def pump():
        try:
            for line in stdin:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    last = time.monotonic()
    while True:
        try:
            line = lines.get(timeout=min(check_every, idle))
        except queue.Empty:
            if time.monotonic() - last >= idle:
                return 0
            view.check_credential()
            continue
        if line is None:
            return 0
        if not line.strip():
            continue
        last = time.monotonic()
        # ensure_ascii (the default) keeps every reply plain ASCII, so memory
        # text crosses the pipe intact whatever encoding either end assumes.
        out.write(json.dumps(_handle(view, line)) + "\n")
        out.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m compartment.vaultview")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--once", action="store_true",
                    help="print one snapshot and exit")
    ap.add_argument("--limit", type=int, default=RECENT_DEFAULT)
    args = ap.parse_args(argv)
    # The replies are the protocol. Anything else that prints - a library,
    # a notice - goes to stderr, where it cannot be mistaken for one.
    reply = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    sys.stdout = sys.stderr
    if args.once:
        reply.write(json.dumps(VaultView(args.vault).snapshot(args.limit))
                    + "\n")
        reply.flush()
        return 0
    return serve(args.vault, out=reply)


if __name__ == "__main__":
    sys.exit(main())
