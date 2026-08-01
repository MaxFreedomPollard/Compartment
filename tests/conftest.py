import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from compartment.vault import Vault  # noqa: E402

PASS = "CorrectHorse"


def seed_pack_bytes() -> bytes:
    return (SRC / "compartment" / "data" / "starter.mpack").read_bytes()


@pytest.fixture()
def vault_path(tmp_path):
    return str(tmp_path / "test.vault")


@pytest.fixture(autouse=True)
def no_real_supervisors(monkeypatch, tmp_path):
    """No test talks to the machine's own systemd or Task Scheduler, or
    writes a start-at-login entry into the real home directory.

    Running the suite must not change the computer it runs on. Without this,
    a Linux box with a user manager would have `set_login` tests enable a
    real user service on it and a Windows box would gain a real logon task,
    installed by whichever test happened to run and removed by nobody - and
    the autostart tests, which patch only sys.platform, would write into the
    real ~/.config on any machine at all.

    Tests that are about those code paths override this with their own fake
    or their own XDG_CONFIG_HOME, which is what monkeypatch ordering gives
    them for free.
    """
    from compartment import systray
    monkeypatch.setattr(systray, "_systemctl",
                        lambda *a: (1, "Failed to connect to bus: No such "
                                       "file or directory"))
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: (1, "ERROR: The system cannot find the "
                                       "file specified."))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture()
def vault(vault_path):
    v = Vault.create(vault_path, PASS, creator="test")
    yield v


@pytest.fixture(scope="session")
def seeded_vault_path(tmp_path_factory):
    from compartment import packs
    p = str(tmp_path_factory.mktemp("seeded") / "seeded.vault")
    v = Vault.create(p, PASS, creator="test")
    packs.seed_records(v, seed_pack_bytes(), caller="test")
    v.lock()
    return p


@pytest.fixture()
def seeded_vault(seeded_vault_path):
    return Vault.unlock(seeded_vault_path, passphrase=PASS)
