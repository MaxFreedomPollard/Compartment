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


@pytest.mark.parametrize("argv", [
    ["serve", "--vault", "{vp}", "--caller", "cursor", "--assert-offline"],
    ["--caller", "cursor", "serve", "--vault", "{vp}", "--assert-offline"],
    ["--vault", "{vp}", "--caller", "cursor", "--assert-offline", "serve"],
])
def test_the_vault_and_caller_may_follow_the_subcommand(tmp_path, monkeypatch,
                                                        argv):
    """`compartment serve --vault PATH` is the order MCP configs are written
    in by hand and by registries, and argparse refused it: "unrecognized
    arguments: --vault", and the client showed a server that would not
    start."""
    vp = str(tmp_path / "v.vault")
    seen = []
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.append(args))
    monkeypatch.setattr(cli.offline_guard, "activate", lambda **k: None)
    cli.main([a.replace("{vp}", vp) for a in argv])
    (args,) = seen
    assert (args.vault, args.caller, args.assert_offline) == (vp, "cursor",
                                                              True)


def test_a_subcommand_without_them_keeps_the_top_level_values(tmp_path,
                                                              monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.append(args))
    cli.main(["--vault", str(tmp_path / "v.vault"), "serve"])
    assert seen[0].vault == str(tmp_path / "v.vault")
    assert seen[0].caller == "user" and seen[0].assert_offline is False


# ------------------------------------------------------- import and export --
@pytest.fixture()
def open_vault(vault, vault_path, monkeypatch):
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)
    vault.lock()
    return vault_path


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_an_import_saved_by_a_windows_tool_is_read(open_vault, tmp_path,
                                                   capsys, encoding):
    """PowerShell 5.1 writes UTF-8 with a BOM, or UTF-16 for `>`: the first
    ended in "Unexpected UTF-8 BOM", the second in UnicodeDecodeError."""
    f = tmp_path / "memories.jsonl"
    f.write_text('{"text": "The staging database runs Postgres 16"}\n',
                 encoding=encoding)
    cli.main(["--vault", open_vault, "import", str(f)])
    assert "imported 1 record" in capsys.readouterr().out


@pytest.mark.parametrize("body, said", [
    ('{"text": "fine"}\nnot json\n', "line 2: not JSON"),
    ('{"txt": "misspelt"}\n', 'line 1: a record needs a "text" string'),
    ("[1, 2]\n", 'line 1: a record needs a "text" string'),
])
def test_an_import_that_is_not_records_is_refused_by_line(open_vault,
                                                          tmp_path, capsys,
                                                          body, said):
    f = tmp_path / "memories.jsonl"
    f.write_text(body, encoding="utf-8")
    err = _ends(["--vault", open_vault, "import", str(f)], capsys)
    assert said in err and "Traceback" not in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_plaintext_export_is_readable_by_its_owner_alone(open_vault,
                                                          tmp_path, capsys):
    """Every memory in the clear, credentials included, was written with the
    umask's 0644: any account on the machine could read an export left in
    /tmp or in a home other users can enter."""
    out = tmp_path / "export.jsonl"
    out.write_text("an older export\n")
    os.chmod(out, 0o644)
    cli.main(["--vault", open_vault, "export", str(out), "--plaintext"])
    assert os.stat(out).st_mode & 0o777 == 0o600
    assert "an older export" not in out.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_an_atomize_listing_is_readable_by_its_owner_alone(open_vault,
                                                          tmp_path, capsys):
    """`atomize --out` writes the over-limit memories decrypted, behind the
    same --plaintext confirmation as export, and with the umask's mode."""
    out = tmp_path / "blobs.jsonl"
    cli.main(["--vault", open_vault, "atomize", "--out", str(out),
              "--plaintext"])
    assert os.stat(out).st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize("command", [
    ["lock", "--sign", "--identity", "{identity}"],
    ["pack", "build", "{source}", "--name", "notes", "--identity",
     "{identity}", "--out", "{out}"],
])
def test_a_new_signing_identity_is_readable_by_its_owner_alone(
        open_vault, tmp_path, capsys, command):
    """identity.json holds the Ed25519 seed that signs packs and sealed
    vaults, and it was written with the umask's 0644: any local account
    could have signed packs this user trusts."""
    identity = tmp_path / "keys" / "identity.json"
    source = tmp_path / "notes.jsonl"
    source.write_text('{"text": "The staging database runs Postgres 16"}\n',
                      encoding="utf-8")
    cli.main(["--vault", open_vault, *[
        a.format(identity=identity, source=source,
                 out=tmp_path / "notes.mpack") for a in command]])
    assert os.stat(identity).st_mode & 0o777 == 0o600
    assert "generated signing identity" in capsys.readouterr().out
