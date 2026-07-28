"""Passphrase attempts are paced: a flat wait, no escalation, no lockout.

The wait is deliberately flat. An escalating delay or a lockout would hand an
attacker a denial of service against the only person who can ever open the
file, since there is no recovery path and no administrator to appeal to.
"""
import time

import pytest

from compartment import crypto
from compartment.crypto import CryptoError, TamperError
from compartment.vault import Vault

PASS = "CorrectHorse"


@pytest.fixture(autouse=True)
def _clean_pacing():
    """Never let one test's failure pace another test."""
    crypto.reset_attempt_pacing()
    yield
    crypto.reset_attempt_pacing()


@pytest.fixture()
def fast(monkeypatch):
    """A short interval so the tests measure behaviour, not wall clock."""
    monkeypatch.setattr(crypto, "ATTEMPT_INTERVAL_SECONDS", 0.4)
    return 0.4


def test_the_shipped_interval_is_five_seconds():
    assert crypto.ATTEMPT_INTERVAL_SECONDS == 5.0


def test_first_attempt_is_not_delayed(vault_path, fast):
    """State, not stopwatch. Wall clock cannot express "was not paced": the
    unlock itself runs Argon2, which is meant to be slow, and on a loaded CI
    runner that alone outlasts any small bound. Asserting no pacing is
    pending says exactly what is meant and cannot flake."""
    Vault.create(vault_path, PASS, creator="t").lock()
    assert crypto._last_failed_attempt is None
    Vault.unlock(vault_path, passphrase=PASS).lock()
    assert crypto._last_failed_attempt is None


def test_a_failed_attempt_paces_the_next_one(vault_path, fast):
    Vault.create(vault_path, PASS, creator="t").lock()
    with pytest.raises((TamperError, CryptoError)):
        Vault.unlock(vault_path, passphrase="wrong")
    start = time.monotonic()
    with pytest.raises((TamperError, CryptoError)):
        Vault.unlock(vault_path, passphrase="wrong-again")
    assert time.monotonic() - start >= fast


def test_the_wait_does_not_escalate(vault_path, fast):
    """Attempt 5 waits exactly as long as attempt 2. No backoff, no lockout."""
    Vault.create(vault_path, PASS, creator="t").lock()
    waits = []
    for i in range(5):
        start = time.monotonic()
        with pytest.raises((TamperError, CryptoError)):
            Vault.unlock(vault_path, passphrase=f"wrong-{i}")
        waits.append(time.monotonic() - start)
    later = waits[1:]
    assert max(later) < min(later) + fast, (
        f"the wait grew across attempts: {later}")


def test_no_lockout_the_right_passphrase_still_opens_after_many_failures(
        vault_path, fast):
    Vault.create(vault_path, PASS, creator="t").lock()
    for i in range(6):
        with pytest.raises((TamperError, CryptoError)):
            Vault.unlock(vault_path, passphrase=f"wrong-{i}")
    v = Vault.unlock(vault_path, passphrase=PASS)   # not locked out
    assert v.status()["locked"] is False
    v.lock()


def test_a_correct_attempt_clears_the_pacing(vault_path, fast):
    Vault.create(vault_path, PASS, creator="t").lock()
    with pytest.raises((TamperError, CryptoError)):
        Vault.unlock(vault_path, passphrase="wrong")
    assert crypto._last_failed_attempt is not None, "a failure must pace"
    Vault.unlock(vault_path, passphrase=PASS).lock()   # pays the wait once
    # the next attempt is not paced, which is a fact about state rather than
    # about how long an Argon2 unlock happens to take on a busy machine
    assert crypto._last_failed_attempt is None


def test_the_user_is_told_why_it_is_waiting(vault_path, fast, capsys):
    Vault.create(vault_path, PASS, creator="t").lock()
    with pytest.raises((TamperError, CryptoError)):
        Vault.unlock(vault_path, passphrase="wrong")
    with pytest.raises((TamperError, CryptoError)):
        Vault.unlock(vault_path, passphrase="wrong-again")
    err = capsys.readouterr().err
    assert "Waiting" in err and "passphrase attempt" in err
