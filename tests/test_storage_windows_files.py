"""Files a Windows editor or shell wrote.

Notepad's "UTF-8 with BOM", Windows PowerShell 5.1's Set-Content and
Out-File (-Encoding UTF8) put a byte order mark in front of a file, and
that PowerShell's `>` redirection writes UTF-16. Each used to fail on its
first line: an access-rules file the user had just edited stopped every
unlock, and an export they had passed through PowerShell would not import.
"""
import codecs
import json

import pytest

from compartment import curate
from compartment.acl import AclError, VaultConfig
from compartment.vault import Vault

from conftest import PASS

CONFIG = {"callers": {"*": {"default_namespace": "main",
                            "grants": {"*": "rw"}},
                      "reader": {"default_namespace": "main",
                                 "grants": {"*": "ro"}}},
          "settings": {"auto_lock_minutes": 12}}


@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE),
    ("utf-16-be", codecs.BOM_UTF16_BE), ("utf-8", b"")])
def test_a_config_saved_by_a_windows_editor_opens(vault, vault_path,
                                                  encoding, bom):
    vault.lock()
    with open(VaultConfig.path_for(vault_path), "wb") as f:
        f.write(bom + json.dumps(CONFIG, indent=2).encode(encoding))
    cfg = VaultConfig.load(vault_path)
    assert cfg.settings["auto_lock_minutes"] == 12
    assert cfg.grant_for("reader", "main") == "ro"
    v = Vault.unlock(vault_path, passphrase=PASS)
    with pytest.raises(AclError):
        v.store("A reader may not write here.", caller="reader")


@pytest.mark.parametrize("content,why", [
    (b'{"callers": {', "not valid JSON"),
    (b'["not", "an", "object"]', "must be a JSON object"),
    (b'{"callers": [], "settings": {}}', "must be a JSON object"),
])
def test_a_config_that_cannot_be_used_is_refused_with_what_to_do(
        vault_path, content, why):
    """Refused, not guessed at: it holds the access rules. The message
    names the file and says what moving it aside would grant."""
    with open(VaultConfig.path_for(vault_path), "wb") as f:
        f.write(content)
    with pytest.raises(AclError) as caught:
        VaultConfig.load(vault_path)
    msg = str(caught.value)
    assert why in msg and VaultConfig.path_for(vault_path) in msg
    assert "move it aside" in msg
    assert isinstance(caught.value, ValueError), \
        "still what a caller catching the old json error catches"


def test_an_export_passed_through_powershell_imports(vault):
    vault.store("The router admin page is at 192.168.1.1.", caller="t")
    exported = vault.export_jsonl()
    vault.forget(vault.db.conn.execute("SELECT id FROM records")
                 .fetchone()["id"], caller="t")
    assert vault.import_jsonl("﻿" + exported, caller="t") == 1


def test_a_curation_plan_with_a_byte_order_mark_parses():
    plan = curate.parse_plan('﻿{"id": "abc", "pieces": [{"text": "x"}]}\n')
    assert plan[0]["id"] == "abc"
