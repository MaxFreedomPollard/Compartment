"""How a CLI command ends when it cannot do what was asked: one line on
stderr that names the file or the remedy, and a non-zero exit, never a
traceback. Agents run the CLI from their own shells and read what it says;
a traceback is forty lines to read to the bottom of for the same fact."""
import os

import pytest

from compartment import cli

from conftest import PASS


@pytest.fixture()
def no_credential(monkeypatch):
    from compartment import vault as vault_module
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)


def _ends(argv, capsys, code=1):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == code
    return capsys.readouterr().err


@pytest.mark.parametrize("command", [["search", "anything"], ["recent"],
                                     ["status"], ["verify"],
                                     ["2fa", "status"]])
def test_a_vault_that_is_not_there_is_said_to_be_missing(
        tmp_path, capsys, no_credential, command):
    """With no vault at the path, `search` and the rest said the vault was
    locked and to run `compartment unlock`, which cannot help, and
    `verify` and `2fa status` raised FileNotFoundError."""
    missing = str(tmp_path / "nowhere" / "memory.vault")
    err = _ends(["--vault", missing, *command], capsys)
    assert f"no vault at {missing}" in err and "compartment init" in err
    assert "Traceback" not in err and "locked" not in err


def test_a_file_named_on_the_command_line_that_is_not_there_is_named(
        vault, vault_path, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)
    vault.lock()
    for argv in (["import", str(tmp_path / "missing.jsonl")],
                 ["pack", "install", str(tmp_path / "missing.mpack")]):
        err = _ends(["--vault", vault_path, *argv], capsys)
        assert err.startswith("error: ") and "missing." in err
        assert len(err.strip().splitlines()) == 1


class _ReaderGone:
    """stdout once the program reading it has exited: `| head`."""

    def __init__(self, fd):
        self.fd = fd

    def write(self, text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")

    def fileno(self):
        return self.fd


def test_output_cut_short_by_its_reader_is_not_a_traceback(tmp_path, capsys,
                                                           monkeypatch):
    """`compartment recent --all | head` ended in BrokenPipeError twice
    over: once from the print, again from the flush at exit."""
    with open(tmp_path / "stdout", "w") as stand_in:
        monkeypatch.setattr("sys.stdout", _ReaderGone(stand_in.fileno()))
        err = _ends(["embed-daemon", "status"], capsys)
    assert err == ""


def test_ctrl_c_ends_a_command_without_a_traceback(monkeypatch, capsys):
    def interrupted(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "cmd_embed_daemon", interrupted)
    err = _ends(["embed-daemon", "status"], capsys, code=130)
    assert err.strip() == "interrupted"


def test_a_vault_found_but_unreadable_names_the_file(vault, vault_path,
                                                     capsys, monkeypatch):
    if getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("root reads any file")
    vault.lock()
    os.chmod(vault_path, 0)
    try:
        err = _ends(["--vault", vault_path, "verify"], capsys)
    finally:
        os.chmod(vault_path, 0o600)
    assert vault_path in err and "Traceback" not in err
