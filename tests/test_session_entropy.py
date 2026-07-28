"""The session credential must not be openable from public values alone.

The wrap key used to be sha256 over a source constant, the boot timestamp,
the uid and the machine id. Every one of those is public or low entropy - the
boot timestamp comes from a small range - so anyone who obtained a copy of the
session file, from a backup, a snapshot or a disk image, could unwrap the
master key offline with a few million guesses, at any later date.

It is now HMAC-SHA256 keyed by 32 random bytes that live only in a volatile
kernel holder (a POSIX shared-memory object; a volatile registry key on
Windows) and are never written to any filesystem. These tests hold both ends
of that: the public values must be useless without the secret, and the secret
must genuinely die with the boot.
"""
import hashlib
import os
import pathlib
import subprocess
import sys

import pytest

from compartment import crypto, session, wire
from compartment.crypto import CryptoError, TamperError

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
KEY = bytes(range(32))
VAULT = "/tmp/entropy-test.vault"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """A session store of our own, and no per-boot secret left behind.

    The teardown runs before monkeypatch's, so the env var still points here
    and the secret destroyed is this test's, never the real one."""
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))
    monkeypatch.delenv("COMPARTMENT_PASSPHRASE", raising=False)
    yield tmp_path / "sess"
    session._forget_boot_secret()


def _old_style_key(token: str, boot: str, uid: str, mid: str) -> bytes:
    """Exactly the derivation this change replaced."""
    return hashlib.sha256("|".join((token, boot, uid, mid)).encode()).digest()


def _wrapped(path: str) -> bytes:
    import json
    doc = json.loads(session._file_for(path).read_text(encoding="utf-8"))
    return bytes.fromhex(doc["wrapped"])


# --- the secret is real, and it is what the derivation hangs on ------------

def test_the_boot_secret_is_32_random_bytes_stable_within_a_boot():
    first = session._boot_secret(create=True)
    assert isinstance(first, bytes) and len(first) == session.SECRET_LEN
    assert first != bytes(session.SECRET_LEN)
    assert session._boot_secret(create=False) == first, "must be stable"
    assert len(set(first)) > 8, "not plausibly random"


def test_a_new_boot_secret_is_a_different_secret():
    first = session._boot_secret(create=True)
    session._forget_boot_secret()
    assert session._boot_secret(create=False) is None    # genuinely gone
    assert session._boot_secret(create=True) != first


def test_the_wrap_key_depends_on_the_secret(monkeypatch):
    """Public inputs held constant, a different secret gives a different key.
    If this fails the secret is not actually feeding the derivation."""
    monkeypatch.setattr(session, "_boot_time", lambda: "1785048716")
    monkeypatch.setattr(session, "machine_id", lambda: "fixed-machine-id")
    assert session._boot_key(b"a" * 32) != session._boot_key(b"b" * 32)


def test_the_secret_is_not_stored_beside_the_session_file(isolated_store):
    """Whoever copies the session file must not copy the key with it."""
    secret = session._boot_secret(create=True)
    session.store(VAULT, KEY)
    for f in isolated_store.rglob("*"):
        if f.is_file():
            blob = f.read_bytes()
            assert secret not in blob
            assert secret.hex().encode() not in blob
    assert not session._holder().startswith(str(isolated_store))


# --- the attack the old derivation permitted ------------------------------

def test_public_values_alone_cannot_unwrap_the_credential(monkeypatch):
    """The attacker has the file, the source, and every public input exactly:
    the real boot time, uid and machine id, plus a sweep of neighbouring boot
    times. That was enough before. It has to be worth nothing now."""
    session.store(VAULT, KEY)
    blob = _wrapped(VAULT)
    aad, legacy_aad = wire.session(session._canon(VAULT))
    real_boot = int(session._boot_time())
    uid, mid = session._uid(), session.machine_id()

    tried = 0
    for token in (wire.SESSION_TOKEN, *wire.SESSION_TOKEN_LEGACY):
        for boot in range(real_boot - 1000, real_boot + 1001):
            for candidate in (_old_style_key(token, str(boot), uid, mid),):
                tried += 1
                with pytest.raises(TamperError):
                    crypto.unseal_which(candidate, blob, aad, legacy_aad)
    assert tried > 2000, "the sweep has to be a real sweep"


def test_guessing_the_secret_is_the_only_way_in(monkeypatch):
    """Every public value is correct; only the 32 secret bytes are guessed."""
    session.store(VAULT, KEY)
    blob = _wrapped(VAULT)
    aad, legacy_aad = wire.session(session._canon(VAULT))
    for guess in (bytes(32), b"\xff" * 32, os.urandom(32), os.urandom(32),
                  hashlib.sha256(b"the machine id").digest()):
        with pytest.raises(TamperError):
            crypto.unseal_which(session._boot_key(guess), blob, aad, legacy_aad)


# --- lifetime: one boot, every process of it ------------------------------

def test_a_stored_credential_round_trips_within_a_boot():
    session.store(VAULT, KEY)
    assert session.get(VAULT) == KEY
    assert session.get(VAULT) == KEY


def test_a_separate_process_opens_the_same_credential(isolated_store):
    """The holder has to be shared, or one unlock would not outlive the
    process that did it. This is a real second interpreter, not a fixture."""
    session.store(VAULT, KEY)
    env = dict(os.environ, COMPARTMENT_SESSION_DIR=str(isolated_store),
               PYTHONPATH=str(SRC))
    out = subprocess.run(
        [sys.executable, "-c",
         "from compartment import session;"
         f"print(session.get({VAULT!r}).hex())"],
        capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == KEY.hex()


def test_a_restart_invalidates_the_credential_even_at_the_same_boot_time():
    """The relock guarantee no longer rests on the boot clock alone: with
    every public value unchanged, losing the secret is enough."""
    p = session.store(VAULT, KEY)
    assert session.get(VAULT) == KEY
    session._forget_boot_secret()              # what a restart does
    assert session.get(VAULT) is None
    assert not p.exists(), "a dead credential must be deleted on sight"


def test_a_credential_from_an_older_build_is_discarded():
    """No migration shims: an unreadable session file is simply discarded."""
    p = session.store(VAULT, KEY)
    p.write_text('{"vault": "/tmp/entropy-test.vault", "wrapped": "00ff"}',
                 encoding="utf-8")
    assert session.get(VAULT) is None
    assert not p.exists()


def test_clear_destroys_the_secret_once_nothing_can_use_it():
    session.store(VAULT, KEY)
    assert session.clear(VAULT) is True
    assert session._boot_secret(create=False) is None


def test_clear_keeps_the_secret_while_another_vault_still_needs_it():
    session.store(VAULT, KEY)
    session.store("/tmp/entropy-other.vault", KEY)
    secret = session._boot_secret(create=False)
    session.clear(VAULT)
    assert session._boot_secret(create=False) == secret
    assert session.get("/tmp/entropy-other.vault") == KEY


# --- file permissions ------------------------------------------------------

def test_the_file_is_created_0600_under_any_umask():
    old = os.umask(0o000)                     # the friendliest possible umask
    try:
        p = session.store(VAULT, KEY)
    finally:
        os.umask(old)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_an_existing_wider_file_is_narrowed_by_the_write():
    """os.open with a mode does nothing to a file that already exists, so the
    write has to narrow it as well as create it narrow."""
    p = session._file_for(VAULT)
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o666)
    session.store(VAULT, KEY)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert session.get(VAULT) == KEY


# --- failures that must not destroy a good credential ---------------------

def test_a_read_error_does_not_destroy_a_valid_credential(monkeypatch):
    """An unrelated OSError used to unlink the credential, relocking a vault
    over a transient failure. It must now say so and touch nothing."""
    session.store(VAULT, KEY)
    path = session._file_for(VAULT)
    real_read = pathlib.Path.read_text

    def boom(self, *a, **k):
        if self == path:
            raise OSError("transient I/O error")
        return real_read(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", boom)
    with pytest.raises(CryptoError):
        session.get(VAULT)
    assert path.is_file(), "the credential must still be there"
    monkeypatch.setattr(pathlib.Path, "read_text", real_read)
    assert session.get(VAULT) == KEY


def test_no_boot_clock_is_a_cryptoerror_not_a_stray_runtimeerror(monkeypatch):
    """Callers only handle CryptoError. A RuntimeError out of boot_time()
    used to sail past them, and out of get() it also deleted the file."""
    session.store(VAULT, KEY)
    path = session._file_for(VAULT)

    def no_clock():
        raise RuntimeError("Cannot determine boot time on this platform")

    monkeypatch.setattr(session, "_boot_time", no_clock)
    with pytest.raises(CryptoError):
        session.get(VAULT)
    assert path.is_file()
    with pytest.raises(CryptoError):
        session.store(VAULT, KEY)


@pytest.mark.skipif(session.IS_WINDOWS, reason="POSIX holder")
def test_a_platform_with_no_holder_refuses_to_store(monkeypatch):
    """Loudly, and without falling back to something recomputable."""
    def no_shm():
        raise CryptoError("No POSIX shared memory (shm_open) on this system")

    monkeypatch.setattr(session, "_libc", no_shm)
    with pytest.raises(CryptoError):
        session.store(VAULT, KEY)
