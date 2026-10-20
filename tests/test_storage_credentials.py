"""Which credential opens the vault when the session store itself fails.

The boot-session store needs shared memory, a readable session directory
and an identifiable boot. Containers, sandboxes and Android lack one or
another, and the error for that tells the user to set COMPARTMENT_PASSPHRASE;
the lookup raised before it ever reached the variable.
"""
import pytest

from compartment import session, vault as vault_mod
from compartment.crypto import CryptoError
from compartment.vault import Vault

from conftest import PASS

BOOT_ERROR = ("Cannot identify this boot session (no boot clock), so a "
              "stored credential cannot be checked. Use COMPARTMENT_PASSPHRASE "
              "or `compartment unlock --once`.")


@pytest.fixture()
def broken_session_store(monkeypatch):
    def get(path):
        raise CryptoError(BOOT_ERROR)
    monkeypatch.setattr(session, "get", get)
    monkeypatch.setattr(vault_mod, "keychain_get", lambda path: None)
    monkeypatch.delenv("COMPARTMENT_PASSPHRASE", raising=False)
    monkeypatch.delenv("ENGRAM_PASSPHRASE", raising=False)


@pytest.mark.parametrize("var", ["COMPARTMENT_PASSPHRASE",
                                 "ENGRAM_PASSPHRASE"])
def test_a_passphrase_in_the_environment_is_used_when_the_session_store_fails(
        broken_session_store, vault_path, monkeypatch, var):
    Vault.create(vault_path, PASS, creator="t").lock()
    monkeypatch.setenv(var, PASS)
    assert Vault.find_credential(vault_path) == (PASS, None)
    pw, key = Vault.resolve_credential(vault_path)
    assert Vault.unlock(vault_path, passphrase=pw, raw_key=key).db.count() == 0


def test_with_nothing_else_the_session_error_still_surfaces(
        broken_session_store, vault_path):
    """Not reported as "locked": the status panel must not say a vault is
    locked when the truth is that its credential store cannot be read."""
    with pytest.raises(CryptoError, match="Cannot identify this boot"):
        Vault.find_credential(vault_path)


def test_a_keychain_credential_is_used_when_the_session_store_fails(
        broken_session_store, vault_path, monkeypatch):
    monkeypatch.setattr(vault_mod, "keychain_get", lambda path: b"k" * 32)
    assert Vault.find_credential(vault_path) == (None, b"k" * 32)
