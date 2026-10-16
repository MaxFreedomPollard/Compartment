"""The command line on a vault: what a command leaves on disk, what it
prints, and what it does when nobody is there to answer."""
import io
import json
import os
import subprocess
import sys

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


LOCKED = ("vault is locked: run `compartment unlock`, or set "
          "COMPARTMENT_PASSPHRASE")


def test_a_command_nobody_can_answer_exits_instead_of_waiting(vault_path,
                                                              tmp_path):
    """With no stored credential every command fell back to getpass, which
    reads stdin when there is no terminal. On an open pipe it waited for
    good: a CI step, a supervisor, a scheduled job (the Windows CI job lost
    2h40m to it). Here the child has no controlling terminal and its stdin
    is a pipe nobody writes to or closes."""
    Vault.create(vault_path, PASS, creator="test").lock()
    env = {k: v for k, v in os.environ.items()
           if k not in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE")}
    env["HOME"] = str(tmp_path / "home")
    detach = ({"creationflags": subprocess.CREATE_NO_WINDOW}
              if sys.platform == "win32" else {"start_new_session": True})
    with open(tmp_path / "out", "wb") as out, \
            open(tmp_path / "err", "wb") as err:
        child = subprocess.Popen(
            [sys.executable, "-m", "compartment.cli", "--vault", vault_path,
             "search", "anything"], stdin=subprocess.PIPE, stdout=out,
            stderr=err, env=env, **detach)
        try:
            rc = child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            pytest.fail("still waiting for a passphrase after 60 s")
        finally:
            child.stdin.close()
    said = (tmp_path / "err").read_text(encoding="utf-8", errors="replace")
    assert rc != 0 and LOCKED in said, said


def _pack_source(tmp_path):
    src = tmp_path / "pack.jsonl"
    src.write_text(json.dumps({"text": "The office printer is on floor 3"})
                   + "\n", encoding="utf-8")
    return str(src)


@pytest.mark.parametrize("command, needs_open, says", [
    (["search", "anything"], False, LOCKED),
    (["reindex", "--re-embed"], False, LOCKED),
    (["rekey"], True, "--new-passphrase-stdin required when not interactive"),
    (["pack", "build", "{source}", "--name", "p", "--encrypt",
      "--identity", "{identity}"], False, "needs a terminal"),
    (["pack", "install", "{source}", "--encrypted"], True,
     "needs a terminal"),
])
def test_no_prompt_is_shown_where_nobody_can_answer_it(
        command, needs_open, says, vault_path, tmp_path, monkeypatch,
        capsys):
    """The four other prompts had the same gap as the vault's own."""
    Vault.create(vault_path, PASS, creator="test").lock()
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    if needs_open:
        monkeypatch.setenv("COMPARTMENT_PASSPHRASE", PASS)

    def prompted(prompt=""):
        raise AssertionError(f"prompted with nobody to answer: {prompt!r}")
    monkeypatch.setattr(cli.getpass, "getpass", prompted)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    argv = [a.format(source=_pack_source(tmp_path),
                     identity=tmp_path / "identity.json") for a in command]
    with pytest.raises(SystemExit) as e:
        cli.main(["--vault", vault_path, *argv])
    assert e.value.code != 0
    assert says in capsys.readouterr().err


def test_a_person_at_a_terminal_is_still_asked(vault_path, monkeypatch,
                                               capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    Vault.create(vault_path, PASS, creator="test").lock()
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASS)
    monkeypatch.setattr(sys, "stdin", Terminal(""))
    cli.main(["--vault", vault_path, "search", "anything", "--json"])
    assert "results" in json.loads(capsys.readouterr().out)


def _status(vault_path, capsys):
    capsys.readouterr()
    cli.main(["--vault", vault_path, "status"])
    return json.loads(capsys.readouterr().out)


def test_status_opens_a_two_factor_vault_as_every_other_command_does(
        vault_path, tmp_path, passphrase_in_env, capsys):
    """status unlocked with no keyfile, so a two-factor vault opened by
    COMPARTMENT_PASSPHRASE showed as locked while `search`, which passes
    the keyfile the vault recorded, opened it."""
    import secrets
    v = Vault.create(vault_path, PASS, creator="test")
    keyfile = tmp_path / "compartment-2fa.key"
    keyfile.write_bytes(secrets.token_bytes(32))
    v.twofa_enable(PASS, keyfile.read_bytes())
    v.config.settings["keyfile_path"] = str(keyfile)
    v.config.save(vault_path)
    v.lock()
    doc = _status(vault_path, capsys)
    assert doc["locked"] is False and "records" in doc, doc


def test_status_says_why_a_credential_does_not_open_the_vault(
        vault_path, monkeypatch, capsys):
    """Every failure to open became {"locked": true} with no message, which
    sends the user to `compartment unlock`. A credential that does not open
    the vault, a model pin mismatch or a damaged file each name their own
    remedy."""
    Vault.create(vault_path, PASS, creator="test").lock()
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    monkeypatch.delenv("ENGRAM_PASSPHRASE", raising=False)
    monkeypatch.setenv("COMPARTMENT_PASSPHRASE", "not the passphrase")
    doc = _status(vault_path, capsys)
    assert doc["locked"] is False, doc
    assert "Wrong passphrase" in doc["error"], doc


def test_status_of_a_vault_with_no_credential_is_locked(vault_path,
                                                        monkeypatch, capsys):
    Vault.create(vault_path, PASS, creator="test").lock()
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    doc = _status(vault_path, capsys)
    assert doc["locked"] is True and doc["vault_id"] and "error" not in doc


@pytest.fixture()
def read_only_dir(tmp_path):
    """A directory nothing can be created in, as on a backup disk or a
    mounted snapshot. Writable again afterwards, so it can be removed."""
    d = tmp_path / "read-only"
    d.mkdir()
    yield d
    os.chmod(d, 0o700)


@pytest.mark.skipif(sys.platform == "win32" or getattr(os, "geteuid",
                                                       lambda: 1)() == 0,
                    reason="Windows and root ignore a directory's permissions")
def test_reads_work_on_a_vault_they_cannot_write(read_only_dir, tmp_path,
                                                 passphrase_in_env, capsys):
    """Opening compacts a journal that holds entries, so a read rewrote the
    vault, and where it could not (a backup disk, a mounted snapshot)
    status, search and export all died with PermissionError."""
    vault_path = str(read_only_dir / "s.vault")
    Vault.create(vault_path, PASS, creator="test").lock()
    v = Vault.unlock(vault_path, passphrase=PASS)
    v.store("The staging database runs Postgres 16", caller="user")
    os.chmod(read_only_dir, 0o500)
    before = open(vault_path, "rb").read()
    status = _status(vault_path, capsys)
    cli.main(["--vault", vault_path, "search", "which Postgres version",
              "--json"])
    out, err = capsys.readouterr()
    found = json.loads(out)
    assert "audit entry for this read was not saved" in err
    exported = tmp_path / "export.jsonl"
    cli.main(["--vault", vault_path, "export", str(exported), "--plaintext"])
    assert status["organic_records"] == 1
    assert any("Postgres 16" in r["text"] for r in found["results"])
    assert "Postgres 16" in exported.read_text(encoding="utf-8")
    assert open(vault_path, "rb").read() == before
