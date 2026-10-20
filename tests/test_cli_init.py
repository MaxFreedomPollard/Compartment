"""`compartment init` finishes on machines that cannot do everything."""
from compartment import cli, session
from compartment.crypto import CryptoError
from compartment.vault import Vault

from conftest import PASS


def test_init_finishes_where_no_unlock_credential_can_be_kept(
        tmp_path, monkeypatch, capsys):
    """Termux, OpenBSD and containers without /dev/shm have no volatile
    holder for the per-boot secret, and session.store says so with
    CryptoError. init died there half-way: the vault made and seeded, but
    not saved after seeding, no agent wired, no app, and exit 1."""
    def no_holder(path, key):
        raise CryptoError("No per-boot secret is available on this system.")
    monkeypatch.setattr(session, "store", no_holder)
    monkeypatch.setattr(cli, "_seed_blobs", lambda: [])
    wired = []
    monkeypatch.setattr(cli, "connect_present_agents",
                        lambda path: wired.append(path) or [])
    path = str(tmp_path / "new.vault")
    cli.main(["--vault", path, "init", "--passphrase", PASS, "--no-app"])
    out = capsys.readouterr().out
    assert "stays locked" in out and "COMPARTMENT_PASSPHRASE" in out
    assert wired == [path]                       # the rest of init ran
    assert "Vault ready" in out
    Vault.unlock(path, passphrase=PASS)
