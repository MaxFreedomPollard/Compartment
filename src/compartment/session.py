"""Boot-session-bound unlock credential.

The lock model Compartment ships by default:

- `compartment unlock` once → the vault stays usable continuously - for weeks or
  months, across logouts/logins, across every new `compartment`/`serve` process.
- Any RESTART or POWER LOSS locks it: the credential is the master key
  wrapped by a key derived from the kernel's boot timestamp (plus uid and
  hostname). A new boot has a new timestamp, so the old wrap can never be
  opened again - the file becomes dead ciphertext and is deleted on sight.
- `compartment lock` (or the MCP panic tool) deletes it immediately.

This is deliberately a CONVENIENCE credential, weaker than the passphrase:
an attacker who can read the session file on the RUNNING, logged-in machine
could also read process memory. Once power is lost, the binding key is gone.
The optional macOS Keychain credential (explicit --keychain) is the stronger
alternative but survives reboots; see SECURITY.md for the comparison.
"""
from __future__ import annotations

from .home import env, home
import hashlib
import json
import os
import stat
from pathlib import Path

from . import crypto, wire
from .crypto import CryptoError, TamperError
from .platforms import boot_time, machine_id


def _session_dir() -> Path:
    d = Path(env("SESSION_DIR", home() / "session"))
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _boot_time() -> str:
    """Seconds-since-epoch of the current boot. Changes on every restart.
    Cross-platform (macOS sysctl / Linux /proc / Windows GetTickCount64)."""
    return boot_time()


def _uid() -> str:
    # os.getuid() is POSIX-only; on Windows fall back to the username.
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid else os.environ.get("USERNAME", "user")


def _boot_key(token: str = wire.SESSION_TOKEN) -> bytes:
    """Wrap key valid only for this boot session of this user on this
    machine. Uses the stable hardware machine id, NOT the hostname -
    macOS renames the host per network, which must not relock the vault."""
    return hashlib.sha256("|".join((
        token,
        _boot_time(),
        _uid(),
        machine_id(),
    )).encode()).digest()


def _canon(vault_path: str) -> str:
    """Canonical spelling of a vault path for keying the credential.

    On case-insensitive filesystems (Windows) one physical vault has many
    valid spellings - the drive letter's case differs between shells
    (``C:\\`` vs ``c:\\``) - which would otherwise hash to different session
    files and different AAD, so an `compartment unlock` in one shell would look
    locked in another. normcase folds those spellings; it is a no-op on POSIX,
    so existing POSIX session files keep matching."""
    return os.path.normcase(os.path.abspath(vault_path))


def _file_for(vault_path: str) -> Path:
    h = hashlib.sha256(_canon(vault_path).encode()).hexdigest()[:16]
    return _session_dir() / f"{h}.session"


def store(vault_path: str, master_key: bytes) -> Path:
    """Persist a boot-bound unlock credential for this vault."""
    p = _file_for(vault_path)
    blob = crypto.seal(_boot_key(), master_key,
                       aad=wire.session(_canon(vault_path))[0])
    p.write_text(json.dumps({"vault": _canon(vault_path),
                             "wrapped": blob.hex()}), encoding="utf-8")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return p


def get(vault_path: str) -> bytes | None:
    """Return the master key if a credential exists AND we are still in the
    same boot session; otherwise remove the stale file and return None.

    A credential written before 2.2 is bound to the older token and sealed
    under the older associated data. It is accepted and then rewritten in the
    current form, so upgrading Compartment does not relock a vault that was
    already open."""
    p = _file_for(vault_path)
    if not p.is_file():
        return None
    canon = _canon(vault_path)
    aad, legacy_aad = wire.session(canon)
    try:
        blob = bytes.fromhex(json.loads(p.read_text(encoding="utf-8"))["wrapped"])
        for token in (wire.SESSION_TOKEN, *wire.SESSION_TOKEN_LEGACY):
            try:
                key, used = crypto.unseal_which(_boot_key(token), blob,
                                                aad, legacy_aad)
            except TamperError:
                continue
            if token != wire.SESSION_TOKEN or used != aad:
                store(vault_path, key)
            return key
        raise TamperError("No boot-session token opened the credential")
    except (TamperError, CryptoError, ValueError, KeyError, OSError):
        # different boot (restart/power loss) or corrupt file → locked
        try:
            p.unlink()
        except OSError:
            pass
        return None


def clear(vault_path: str) -> bool:
    p = _file_for(vault_path)
    if p.is_file():
        p.unlink()
        return True
    return False
