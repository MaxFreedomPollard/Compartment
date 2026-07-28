"""Boot-session-bound unlock credential.

The lock model Compartment ships by default:

- `compartment unlock` once, and the vault stays usable for every later
  `compartment`/`serve` process this user starts, for weeks or months.
- Any RESTART or POWER LOSS locks it again.
- `compartment lock` (or the MCP panic tool) deletes it immediately.

WHAT ACTUALLY BINDS THE CREDENTIAL

The master key is sealed under

    HMAC-SHA256(key = per-boot secret, msg = token | boot time | uid | machine id)

The per-boot secret is 32 bytes from the OS CSPRNG, minted the first time a
credential is stored after a boot and kept in a volatile, kernel-resident
holder that no filesystem backs:

  * macOS and Linux: a POSIX shared-memory object (`shm_open`) owned by this
    uid, mode 0600. It outlives the process that created it, is shared by
    every later process of this user, and dies with the kernel that holds it.
    On Linux that object is a file on the /dev/shm tmpfs; on macOS it has no
    filesystem presence at all.
  * Windows: a REG_OPTION_VOLATILE key under HKEY_CURRENT_USER. Volatile keys
    are never written to the hive on disk and vanish when the hive unloads,
    which is at logoff and at every restart. Windows therefore also relocks
    on logoff, which is stricter than the POSIX platforms.
  * Anywhere else: storing a credential FAILS, loudly, naming the passphrase
    and Keychain alternatives. There is no fallback to a derivation that
    someone holding the file could recompute.

Boot time, uid and machine id are still bound in, but as CONTEXT, not as the
secret. On their own they are worth nothing: guessing all three still leaves
32 unknown bytes.

WHAT THIS PROTECTS AGAINST

A copy of the session file by itself: a backup, a filesystem snapshot, a
cloned or discarded disk, a powered-off stolen laptop, an admin who reads the
file later. What is on disk is ciphertext whose key is not in the file, not in
the directory holding it, and not on any disk anywhere.

WHAT IT DOES NOT PROTECT AGAINST

- Code running as this user on this machine while it is up. It can read the
  per-boot secret exactly the way Compartment does, and it could just as
  easily read the master key out of a running process. This credential is a
  convenience, weaker than the passphrase, and that has not changed.
- root / Administrator / anything in the kernel, for the same reason.
- Anything that captures RAM: a hibernation image, a suspended VM's saved
  state, a swap or pagefile capture. Linux can swap tmpfs pages out (macOS
  encrypts swap by default; Windows may page the registry hive), so a memory
  image taken while the machine was up or asleep can contain the secret, and
  with it the session file becomes openable.
- It is not a second factor. The passphrase is still the only real
  credential; this file only decides whether it has to be retyped.

A RESTART IS FINAL

After a restart nobody can open a credential written before it, Compartment
included: the secret it was keyed with no longer exists anywhere, and the boot
time bound into the context has changed as well. Dead files are deleted on
sight. A credential written by an older build is discarded the same way -
there is no migration, the user simply unlocks once more.

The optional macOS Keychain credential (explicit --keychain) is the persistent
alternative: it survives reboots by design. See SECURITY.md.
"""
from __future__ import annotations

from .home import env, home
import ctypes
import ctypes.util
import errno
import hashlib
import hmac
import json
import mmap
import os
import secrets
import stat
import time
from pathlib import Path

from . import crypto, wire
from .crypto import CryptoError, TamperError
from .platforms import IS_WINDOWS, boot_time, machine_id

# Session file format. Anything else on disk is not ours and is discarded:
# a credential can only ever be opened by the boot that wrote it, so there is
# nothing older to migrate.
FORMAT = 3

SECRET_LEN = 32

# Domain separator for the holder's NAME (not its contents). The name is a
# hash of public values and reveals nothing; it only has to be stable within
# a boot and distinct between users and between session stores.
_HOLDER_TOKEN = "compartment-boot-secret-v1"

# How long to wait for another process that is mid-creation of the secret.
_RACE_TIMEOUT = 2.0


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


# ---------------------------------------------------------------------------
# The per-boot secret: 32 random bytes in a holder a restart destroys.
#
# This is the whole security argument, so the holder has to satisfy three
# things at once. It must not sit on any filesystem that a copy of the session
# file would come with, or the copy brings the key along. It must be readable
# by later processes of this user, or one unlock would not outlive the process
# that did it. And a restart must genuinely destroy it, not merely hide it.
# ---------------------------------------------------------------------------

_libc_cache = None


def _libc():
    """libc/librt with shm_open, argtypes set for the variadic call.

    Only the two FIXED parameters get argtypes. `mode` is variadic, and on
    arm64 macOS variadic arguments are passed on the stack while fixed ones go
    in registers: naming mode in argtypes puts it in the wrong place, the
    object is created with mode 0, and it can then never be reopened - not
    even by its owner. That failure is silent until the next process reads."""
    global _libc_cache
    if _libc_cache is not None:
        return _libc_cache
    for name in (None, "rt", "c"):
        try:
            lib = ctypes.CDLL(ctypes.util.find_library(name) if name else None,
                              use_errno=True)
        except OSError:
            continue
        if not hasattr(lib, "shm_open"):
            continue
        lib.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.shm_open.restype = ctypes.c_int
        lib.shm_unlink.argtypes = [ctypes.c_char_p]
        lib.shm_unlink.restype = ctypes.c_int
        _libc_cache = lib
        return lib
    raise CryptoError(
        "No POSIX shared memory (shm_open) on this system, so there is "
        "nowhere to hold a per-boot secret that a restart destroys. "
        "Compartment will not fall back to a credential that anyone holding "
        "the session file could recompute. Use `compartment unlock "
        "--keychain` (macOS), COMPARTMENT_PASSPHRASE, `compartment unlock "
        "--once`, or `compartment init --no-session`.")


def _shm_open(name: str, flags: int, mode: int | None = None) -> int:
    lib = _libc()
    if mode is None:
        fd = lib.shm_open(name.encode(), flags)
    else:
        fd = lib.shm_open(name.encode(), flags, ctypes.c_int(mode))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), name)
    return fd


def _posix_read(name: str) -> tuple[str, bytes | None]:
    """("ok", secret) | ("absent", None) | ("pending", None).

    "pending" means another process created the object but has not finished
    writing it. That race is real on every fresh boot: two Compartment
    processes can start in the same instant."""
    try:
        fd = _shm_open(name, os.O_RDONLY)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return "absent", None
        raise CryptoError(
            f"Cannot open this boot's secret ({name}): {exc}") from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise CryptoError(
                f"The per-boot secret {name} belongs to uid {st.st_uid}, not "
                f"to you. Refusing to key a credential with it.")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise CryptoError(
                f"The per-boot secret {name} is readable by other users "
                f"(mode {stat.S_IMODE(st.st_mode):o}). Refusing to use it.")
        # macOS rounds the object up to a page, so this is a floor, not ==.
        if st.st_size < SECRET_LEN:
            return "pending", None
        with mmap.mmap(fd, SECRET_LEN, prot=mmap.PROT_READ) as m:
            data = m.read(SECRET_LEN)
    finally:
        os.close(fd)
    # All zero means sized but not yet written. A real secret is all zero with
    # probability 2**-256, so this costs nothing and closes the race.
    return ("ok", data) if any(data) else ("pending", None)


def _posix_create(name: str) -> bytes | None:
    """Mint the secret, or None if another process got there first."""
    try:
        fd = _shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return None
        raise CryptoError(
            f"Cannot create this boot's secret ({name}): {exc}") from exc
    try:
        os.ftruncate(fd, SECRET_LEN)
        secret = secrets.token_bytes(SECRET_LEN)
        with mmap.mmap(fd, SECRET_LEN) as m:
            m.write(secret)
    finally:
        os.close(fd)
    return secret


def _posix_secret(name: str, create: bool) -> bytes | None:
    deadline = time.monotonic() + _RACE_TIMEOUT
    while True:
        state, data = _posix_read(name)
        if state == "ok":
            return data
        if state == "absent":
            if not create:
                return None
            secret = _posix_create(name)
            if secret is not None:
                return secret
        if time.monotonic() > deadline:
            raise CryptoError(
                f"Timed out waiting for another process to finish creating "
                f"this boot's secret ({name}).")
        time.sleep(0.01)


def _posix_forget(name: str) -> bool:
    return _libc().shm_unlink(name.encode()) == 0


# --- Windows: a volatile HKCU key, never written to the hive on disk -------

_REG_OPTION_VOLATILE = 0x00000001
_REG_OPENED_EXISTING_KEY = 2


def _win_read(sub: str) -> bytes | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            value, kind = winreg.QueryValueEx(k, "secret")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CryptoError(
            f"Cannot read this boot's secret ({sub}): {exc}") from exc
    if kind != winreg.REG_BINARY or len(value) != SECRET_LEN:
        raise CryptoError(
            f"The per-boot secret at {sub} is malformed; refusing to use it.")
    return bytes(value)


def _win_secret(sub: str, create: bool) -> bytes | None:
    """Read, or create under REG_OPTION_VOLATILE.

    winreg cannot do this: `CreateKeyEx` has no options parameter, and folding
    REG_OPTION_VOLATILE into its access mask silently creates an ORDINARY key
    (0x1 is KEY_QUERY_VALUE there), which is written to NTUSER.DAT and comes
    back after a reboot. So the create goes through RegCreateKeyExW directly,
    where the option belongs."""
    import winreg
    from ctypes import wintypes
    got = _win_read(sub)
    if got is not None or not create:
        return got
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.RegCreateKeyExW.argtypes = [
        wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(wintypes.HKEY), ctypes.POINTER(wintypes.DWORD)]
    advapi32.RegSetValueExW.argtypes = [
        wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_char_p, wintypes.DWORD]
    advapi32.RegCloseKey.argtypes = [wintypes.HKEY]
    hkey, disp = wintypes.HKEY(), wintypes.DWORD()
    rc = advapi32.RegCreateKeyExW(
        wintypes.HKEY(winreg.HKEY_CURRENT_USER), sub, 0, None,
        _REG_OPTION_VOLATILE, winreg.KEY_READ | winreg.KEY_WRITE, None,
        ctypes.byref(hkey), ctypes.byref(disp))
    if rc != 0:
        raise CryptoError(
            f"Cannot create the volatile registry key {sub} holding this "
            f"boot's secret (error {rc}).")
    try:
        if disp.value == _REG_OPENED_EXISTING_KEY:
            # Another process created the key; the value is its business.
            deadline = time.monotonic() + _RACE_TIMEOUT
            while True:
                got = _win_read(sub)
                if got is not None:
                    return got
                if time.monotonic() > deadline:
                    raise CryptoError(
                        f"Timed out waiting for another process to write this "
                        f"boot's secret ({sub}).")
                time.sleep(0.01)
        secret = secrets.token_bytes(SECRET_LEN)
        rc = advapi32.RegSetValueExW(hkey, "secret", 0, winreg.REG_BINARY,
                                     secret, len(secret))
        if rc != 0:
            raise CryptoError(
                f"Cannot write this boot's secret to {sub} (error {rc}).")
        return secret
    finally:
        advapi32.RegCloseKey(hkey)


def _win_forget(sub: str) -> bool:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        return True
    except OSError:
        return False


# --- holder identity, and the accessors the rest of the module uses --------

def _holder() -> str:
    """Name of this user's per-boot secret for this session store.

    The session directory is folded in so that two stores (a temporary one in
    a test, the real one) never share a secret, and so nothing outside a store
    can be invalidated by a `compartment lock` inside it."""
    digest = hashlib.sha256("|".join(
        (_HOLDER_TOKEN, _uid(), _canon(str(_session_dir())))).encode()
    ).hexdigest()[:24]
    if IS_WINDOWS:
        return rf"Software\Compartment\boot-secret\{digest}"
    # macOS caps a POSIX shm name at 31 characters, the slash included.
    return f"/cmpt.{digest}"


def _boot_secret(*, create: bool) -> bytes | None:
    """This boot's secret, or None if there is none and create is False.

    None is the answer that means "a different boot wrote that credential":
    the holder is empty, so whatever it was keyed with is unrecoverable."""
    name = _holder()
    return _win_secret(name, create) if IS_WINDOWS else _posix_secret(name, create)


def _forget_boot_secret() -> bool:
    """Destroy this boot's secret. Every credential keyed with it dies now.

    Used by `clear()` once nothing is left that it could open, and by the
    tests to simulate a restart without needing one."""
    try:
        name = _holder()
        return _win_forget(name) if IS_WINDOWS else _posix_forget(name)
    except CryptoError:
        return False


def _boot_context(token: str) -> bytes:
    """The public context bound into the wrap key alongside the secret."""
    try:
        return "|".join((token, _boot_time(), _uid(), machine_id())).encode()
    except CryptoError:
        raise
    except Exception as exc:                                  # noqa: BLE001
        # boot_time() raises RuntimeError where the platform has no boot
        # clock, and the subprocesses under it can fail in their own ways.
        # Callers handle CryptoError; anything else escapes them as a crash.
        raise CryptoError(
            f"Cannot identify this boot session ({exc}), so a stored "
            f"credential cannot be checked. Use COMPARTMENT_PASSPHRASE or "
            f"`compartment unlock --once`.") from exc


def _boot_key(secret: bytes, token: str = wire.SESSION_TOKEN) -> bytes:
    """Wrap key for this boot, this user, this machine.

    Keyed by the per-boot secret, so it cannot be rebuilt from anything on
    disk. The machine id is the stable hardware one, NOT the hostname: macOS
    renames the host per network, which must not relock the vault."""
    return hmac.new(secret, _boot_context(token), hashlib.sha256).digest()


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


def _write_private(p: Path, text: str) -> None:
    """Create the file 0600 from the start.

    Not write_text-then-chmod: that publishes the file at whatever the umask
    allows and narrows it afterwards, and anything reading in between wins.
    The fchmod then covers a file that already existed with a wider mode, and
    a umask that would strip owner-write out of the create mode."""
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):        # POSIX; on Windows the ACL is used
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _discard(p: Path) -> None:
    """Delete a credential that can never be opened again."""
    try:
        p.unlink()
    except OSError:
        pass


def store(vault_path: str, master_key: bytes) -> Path:
    """Persist a boot-bound unlock credential for this vault.

    Raises CryptoError if this platform has no volatile holder for the
    per-boot secret. Failing the unlock is the point: the alternative is a
    credential that a copy of the file alone would open."""
    secret = _boot_secret(create=True)
    if secret is None:                                    # defensive
        raise CryptoError("No per-boot secret is available on this system.")
    canon = _canon(vault_path)
    blob = crypto.seal(_boot_key(secret), master_key, aad=wire.session(canon)[0])
    p = _file_for(vault_path)
    _write_private(p, json.dumps({"v": FORMAT, "vault": canon,
                                  "wrapped": blob.hex()}))
    return p


def get(vault_path: str) -> bytes | None:
    """Return the master key if a credential exists AND we are still in the
    boot session that wrote it; otherwise delete the dead file and return None.

    None means locked. A CryptoError means something is wrong that deleting
    the credential would not fix - an unreadable file, no boot clock - and in
    that case nothing is deleted."""
    p = _file_for(vault_path)
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        # A perfectly good credential that we merely failed to read. Deleting
        # it here would relock a vault over a transient error.
        raise CryptoError(
            f"Cannot read the session credential at {p} ({exc}). It has been "
            f"left alone; fix the permissions, or delete it and unlock "
            f"again.") from exc
    try:
        doc = json.loads(raw)
        if doc.get("v") != FORMAT:
            raise ValueError(f"session format {doc.get('v')!r} is not ours")
        blob = bytes.fromhex(doc["wrapped"])
    except (ValueError, TypeError, KeyError, AttributeError):
        _discard(p)               # corrupt, or written by an older build
        return None

    secret = _boot_secret(create=False)
    if secret is None:
        # A different boot wrote this. The secret it was keyed with is gone
        # from the kernel, so nothing can open the file, Compartment included.
        _discard(p)
        return None

    canon = _canon(vault_path)
    aad, legacy_aad = wire.session(canon)
    for token in (wire.SESSION_TOKEN, *wire.SESSION_TOKEN_LEGACY):
        try:
            key, used = crypto.unseal_which(_boot_key(secret, token), blob,
                                            aad, legacy_aad)
        except TamperError:
            continue
        if token != wire.SESSION_TOKEN or used != aad:
            store(vault_path, key)      # rewrite under the current labels
        return key
    _discard(p)
    return None


def clear(vault_path: str) -> bool:
    """Delete this vault's credential.

    If that was the last one in this store, destroy the per-boot secret too:
    keeping it would leave key material in memory that nothing on disk can
    use, and a panic-lock should not leave that lying about."""
    p = _file_for(vault_path)
    existed = p.is_file()
    if existed:
        p.unlink()
    if not any(_session_dir().glob("*.session")):
        _forget_boot_secret()
    return existed
