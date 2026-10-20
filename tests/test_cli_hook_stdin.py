"""`compartment hook capture` reads Claude Code's hook payload as the UTF-8
it is written in, whatever the locale would decode stdin as."""
import io
import json

import pytest

from compartment import cli, session
from compartment.vault import Vault

from conftest import PASS


def test_a_memory_note_under_a_non_ascii_home_is_captured(tmp_path,
                                                          vault_path,
                                                          monkeypatch,
                                                          capsys):
    """On Windows Python decodes a piped stdin with the ANSI code page, so
    the path of a note under C:\\Users\\José\\.claude arrived as
    C:\\Users\\JosÃ©\\.claude and every capture there was dropped."""
    note = (tmp_path / "José" / ".claude" / "projects" / "app" / "memory"
            / "deploy.md")
    note.parent.mkdir(parents=True)
    note.write_text("The deploy key lives in 1Password under 'prod'.\n",
                    encoding="utf-8")
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write",
                          "tool_input": {"file_path": str(note)}},
                         ensure_ascii=False).encode("utf-8")
    # stdin as a Windows pipe presents it: the bytes, decoded as cp1252.
    monkeypatch.setattr("sys.stdin",
                        io.TextIOWrapper(io.BytesIO(payload),
                                         encoding="cp1252"))
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    with pytest.raises(SystemExit) as e:
        cli.main(["--vault", vault_path, "hook", "capture", "--json"])
    assert e.value.code == 0
    res = json.loads(capsys.readouterr().out)
    assert res["stored"] is True, res
