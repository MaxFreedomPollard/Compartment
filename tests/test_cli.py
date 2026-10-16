"""The command line on a vault: what a command leaves on disk, what it
prints, and what it does when nobody is there to answer."""
import pytest

from compartment import cli
from compartment import vault as vault_module
from compartment.vault import Vault

PASS = "CorrectHorse"


@pytest.fixture()
def passphrase_in_env(monkeypatch):
    """The passphrase as a script passes it, and no other credential: no
    keychain lookup, so nothing here reads the machine's own keychain."""
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    monkeypatch.delenv("ENGRAM_PASSPHRASE", raising=False)
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)


def _audited_on_disk(vault_path):
    v = Vault.unlock(vault_path, passphrase=PASS)
    return [r["op"] for r in v.db.conn.execute(
        "SELECT op FROM audit ORDER BY seq")]


def test_get_keeps_the_audit_row_it_writes(vault_path, passphrase_in_env):
    """A read is audited in RAM and reaches disk only with a save. `search`
    saves after it prints; `get` printed and left, so no `compartment get`
    ever reached the audit log."""
    v = Vault.create(vault_path, PASS, creator="test")
    rid = v.store("The staging database runs Postgres 16", caller="test")["id"]
    v.lock()
    cli.main(["--vault", vault_path, "get", rid])
    assert _audited_on_disk(vault_path)[-1] == "get"
