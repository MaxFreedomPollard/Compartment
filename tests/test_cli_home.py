"""Where the CLI finds the vault: `~` expanded wherever a path comes from,
and no home directory needed when the vault is named."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from compartment import cli
from compartment.vault import Vault

from conftest import PASS

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture()
def home_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))          # expanduser, POSIX
    monkeypatch.setenv("USERPROFILE", str(home))   # expanduser, Windows
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)
    Vault.create(str(home / "v.vault"), PASS, creator="test").lock()
    return home


@pytest.mark.parametrize("how", ["flag", "flag after", "COMPARTMENT_VAULT",
                                 "ENGRAM_VAULT"])
def test_a_vault_path_starting_with_a_tilde_is_the_home_one(
        home_dir, monkeypatch, capsys, how):
    """MCP client configs pass "~/..." in args and env literally, since no
    shell runs in between: the CLI looked for a folder named "~" in the
    client's working directory and reported no vault."""
    for name in ("COMPARTMENT_VAULT", "ENGRAM_VAULT"):
        monkeypatch.delenv(name, raising=False)
    argv = ["status"]
    if how == "flag":
        argv = ["--vault", "~/v.vault", "status"]
    elif how == "flag after":
        argv = ["status", "--vault", "~/v.vault"]
    else:
        monkeypatch.setenv(how, "~/v.vault")
    cli.main(argv)
    status = json.loads(capsys.readouterr().out)
    # Compared as paths: JSON escapes a Windows path's backslashes, and
    # expanduser there joins with "/" after the profile folder.
    assert os.path.normcase(os.path.normpath(status["vault"])) == \
        os.path.normcase(os.path.normpath(str(home_dir / "v.vault")))
    assert status["locked"] is False


def _cli_with_no_home(*argv):
    """The CLI in a process where Compartment's home cannot be found, as
    with HOME unset and no password-database entry for the uid."""
    code = ("import sys, compartment.home as h\n"
            "def no_home():\n"
            "    raise RuntimeError('Could not determine home directory.')\n"
            "h.home = no_home\n"
            "from compartment import cli\n"
            "cli.main(sys.argv[1:])\n")
    env = dict(os.environ, COMPARTMENT_EMBED_DAEMON="0",
               PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    for name in ("COMPARTMENT_VAULT", "ENGRAM_VAULT"):
        env.pop(name, None)
    return subprocess.run([sys.executable, "-c", code, *argv],
                          capture_output=True, text=True, env=env, timeout=120)


def test_a_named_vault_needs_no_home_directory(tmp_path):
    """The default vault was a module constant: with no home every command
    died at import, even one that named its vault."""
    missing = str(tmp_path / "nowhere.vault")
    r = _cli_with_no_home("--vault", missing, "status")
    assert "Traceback" not in r.stderr, r.stderr
    assert f"no vault at {missing}" in r.stderr
    r = _cli_with_no_home("--version")
    assert r.returncode == 0 and r.stdout.strip()


def test_no_vault_and_no_home_says_what_to_pass():
    r = _cli_with_no_home("status")
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "--vault PATH" in r.stderr and "COMPARTMENT_VAULT" in r.stderr
