"""Lock semantics: locked by default, one unlock persists across processes
and logins, restart/power loss relocks (boot-bound credential)."""
import pytest

from compartment import crypto, session
from compartment.crypto import CryptoError
from compartment.vault import Vault

from conftest import PASS


@pytest.fixture(autouse=True)
def _isolated_session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "sess"))
    monkeypatch.delenv("COMPARTMENT_PASSPHRASE", raising=False)


def test_locked_by_default_without_credential(vault, vault_path):
    vault.lock()
    with pytest.raises(CryptoError):
        Vault.resolve_credential(vault_path)


def test_session_unlock_persists_across_processes(vault, vault_path):
    session.store(vault_path, vault._master)
    vault.lock()
    # a brand-new "process" (fresh resolution) opens without a passphrase
    pw, key = Vault.resolve_credential(vault_path)
    assert pw is None and key is not None
    v2 = Vault.unlock(vault_path, raw_key=key)
    assert v2.db.count() == 0
    assert v2.status()["locked"] is False


def test_restart_invalidates_session_credential(vault, vault_path, monkeypatch):
    session.store(vault_path, vault._master)
    assert session.get(vault_path) is not None
    # simulate a reboot: the kernel reports a different boot time
    monkeypatch.setattr(session, "_boot_time", lambda: "9999999999")
    assert session.get(vault_path) is None          # credential dead
    with pytest.raises(CryptoError):
        Vault.resolve_credential(vault_path)        # locked again
    # and the stale file was removed - even reverting the clock can't revive it
    monkeypatch.undo()
    assert session.get(vault_path) is None


def test_lock_clears_session_credential(vault, vault_path):
    session.store(vault_path, vault._master)
    assert session.clear(vault_path) is True
    assert session.get(vault_path) is None
    with pytest.raises(CryptoError):
        Vault.resolve_credential(vault_path)


def test_session_file_bound_to_vault_path(vault, vault_path, tmp_path):
    session.store(vault_path, vault._master)
    other = str(tmp_path / "other.vault")
    assert session.get(other) is None


def test_session_wrap_is_aead_not_plaintext(vault, vault_path):
    p = session.store(vault_path, vault._master)
    raw = p.read_text(encoding="utf-8")
    assert vault._master.hex() not in raw  # never stored in the clear


def test_multiprocess_write_safety(vault, vault_path):
    """Second writer on the same vault detects staleness instead of corrupting."""
    from compartment.vault import VaultStaleError
    vault.store("writer A first", caller="A")
    b = Vault.unlock(vault_path, passphrase=PASS)   # second "process"
    b.store("writer B first", caller="B")            # B writes...
    with pytest.raises(VaultStaleError):
        vault.store("writer A second", caller="A")   # ...A is now stale
    a2 = Vault.unlock(vault_path, passphrase=PASS)   # reopen → sees B's write
    assert a2.db.count() == 2
    a2.store("writer A second", caller="A")          # and can write again
    assert a2.db.count() == 3
