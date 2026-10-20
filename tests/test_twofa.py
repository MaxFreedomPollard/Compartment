"""Two-factor unlock: passphrase (knowledge) + keyfile (possession).

Both factors feed Argon2id, so the requirement is cryptographic - there is
no policy check to bypass. Also proves: no credential is ever auto-generated
at create/rekey (the user's passphrase is the only knowledge factor), and
vaults from older versions that carry a legacy recovery slot still open.
"""
import secrets

import pytest

from compartment import crypto
from compartment.crypto import CryptoError, TamperError
from compartment.vault import Vault

from conftest import PASS


def test_create_makes_single_user_passphrase_slot(vault):
    types = [s["type"] for s in vault.header.keyslots]
    assert types == ["passphrase"]      # nothing auto-generated, ever
    with pytest.raises(CryptoError):
        Vault.create(vault.path + ".x", "")   # empty passphrase refused


def test_enable_requires_both_factors_to_unlock(vault, vault_path):
    kf = secrets.token_bytes(32)
    vault.twofa_enable(PASS, kf)
    assert vault.twofa_enabled()
    vault.lock()

    # passphrase alone: refused, with an instructive message
    with pytest.raises(CryptoError, match="keyfile is required"):
        Vault.unlock(vault_path, passphrase=PASS)
    # wrong keyfile: refused before any KDF work, named clearly
    with pytest.raises(CryptoError, match="not this vault's keyfile"):
        Vault.unlock(vault_path, passphrase=PASS,
                     keyfile=secrets.token_bytes(32))
    # wrong passphrase with right keyfile: refused
    with pytest.raises(TamperError):
        Vault.unlock(vault_path, passphrase="wrong", keyfile=kf)
    # both factors: opens
    v2 = Vault.unlock(vault_path, passphrase=PASS, keyfile=kf)
    assert v2.status()["records"] == 0
    assert [s["type"] for s in v2.header.keyslots] == ["passphrase+keyfile"]


def test_enable_verifies_the_passphrase_first(vault):
    with pytest.raises(TamperError):
        vault.twofa_enable("not the passphrase", secrets.token_bytes(32))
    assert not vault.twofa_enabled()


def test_disable_restores_passphrase_only(vault, vault_path):
    kf = secrets.token_bytes(32)
    vault.twofa_enable(PASS, kf)
    vault.twofa_disable(PASS, kf)
    vault.lock()
    v2 = Vault.unlock(vault_path, passphrase=PASS)   # no keyfile needed
    assert [s["type"] for s in v2.header.keyslots] == ["passphrase"]


def test_rekey_with_twofa_keeps_requiring_both(vault, vault_path):
    kf = secrets.token_bytes(32)
    vault.twofa_enable(PASS, kf)
    with pytest.raises(CryptoError, match="needs the keyfile"):
        vault.rekey("NewHorse")                       # keyfile mandatory
    vault.rekey("NewHorse", keyfile=kf)
    vault.lock()
    with pytest.raises(CryptoError):
        Vault.unlock(vault_path, passphrase="NewHorse")   # still 2FA
    v2 = Vault.unlock(vault_path, passphrase="NewHorse", keyfile=kf)
    assert v2.twofa_enabled()


def test_short_keyfile_refused(vault):
    with pytest.raises(CryptoError, match="too short"):
        vault.twofa_enable(PASS, b"tiny")


def test_keyfile_hint_roundtrip(vault, vault_path, tmp_path):
    kf_path = tmp_path / "compartment-2fa.key"
    kf = secrets.token_bytes(32)
    kf_path.write_bytes(kf)
    vault.twofa_enable(PASS, kf)
    vault.config.settings["keyfile_path"] = str(kf_path)
    vault.config.save(vault_path)
    vault.lock()
    assert Vault.load_keyfile_hint(vault_path) == kf
    v2 = Vault.unlock(vault_path, passphrase=PASS,
                      keyfile=Vault.load_keyfile_hint(vault_path))
    assert v2.twofa_enabled()


def test_legacy_recovery_slot_still_opens(vault_path):
    """Vaults created by older Compartment versions carry an auto-generated
    recovery slot; those phrases must keep working (read-path only)."""
    v = Vault.create(vault_path, PASS, creator="legacy-sim")
    slot_rec, words = crypto.make_recovery_slot(v._master)
    v.header.keyslots.append(slot_rec)
    v.save()
    v.lock()
    v2 = Vault.unlock(vault_path, passphrase=" ".join(words))
    assert v2.status()["records"] == 0


def test_a_new_keyfile_is_created_private_and_exclusive(vault, vault_path,
                                                        tmp_path, monkeypatch):
    """`2fa enable` wrote the keyfile and chmod'ed it afterwards, so the
    second factor sat at the umask's mode (0644 on most systems) for that
    moment, and a file planted at the path in between was written through.
    It is now created 0600 with O_EXCL in one call."""
    import os
    from compartment import cli
    vault.lock()
    keyfile = tmp_path / "factor.key"
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        if str(path) == str(keyfile):
            seen.append((flags, mode))
        return real_open(path, flags, mode, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    monkeypatch.delenv("COMPARTMENT_PASSPHRASE", raising=False)
    cli.main(["--vault", vault_path, "2fa", "enable", "--passphrase", PASS,
              "--keyfile", str(keyfile)])
    assert seen, "the keyfile was not created through os.open"
    flags, mode = seen[0]
    assert flags & os.O_EXCL and flags & os.O_CREAT and mode == 0o600
    assert len(keyfile.read_bytes()) == crypto.KEYFILE_LEN
    if os.name != "nt":
        assert (keyfile.stat().st_mode & 0o777) == 0o600
