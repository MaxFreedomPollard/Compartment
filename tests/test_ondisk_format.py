"""The on-disk format is not branding, and must survive every rename.

Compartment was called engRAM until 1.15.0. The strings below are AEAD
associated data, KDF context labels and file-format tags that were written
into every vault created before that. They are wire format. Changing one does
not rename anything - it makes previously written bytes undecryptable.

`engram-keyslot` is the worst of them: it is the associated data used to wrap
the master key. Alter it and the keyslot no longer unseals, which means the
vault cannot be opened again by anyone, with any passphrase, ever. Every
memory in it is gone. `engram-record:`, `engram-record-body:` and
`engram-payload:` are the same hazard one layer down.

This is not hypothetical. The 1.15.0 rename did a find-and-replace across the
source and rewrote all sixteen of them. It surfaced only because the vault
started reporting itself locked; had the session not broken first, the next
person to run it against a real vault would have found their data unreadable
and no error explaining why.

So: these constants are frozen. If the product is renamed again, they stay.
"""
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "compartment"

# module -> the exact literals that must appear in it, verbatim
FROZEN = {
    "crypto.py": [
        'b"engram-keyslot"',            # wraps the master key
        r'b"\x1f engram-2fa \x1f"',     # separates passphrase from keyfile
        'b"engram-record:"',            # per-record key wrapping
    ],
    "store.py": ['b"engram-record-body:"'],
    "vaultfile.py": ['b"engram-payload:"', 'b"engram-journal:"'],
    "packs.py": ['b"engram-pack:"'],
    "session.py": ['"engram-session-v2"', 'b"engram-session:"'],
}


@pytest.mark.parametrize("module,literals",
                         sorted((m, ls) for m, ls in FROZEN.items()))
def test_ondisk_constants_are_frozen(module, literals):
    text = (SRC / module).read_text(encoding="utf-8")
    for lit in literals:
        assert lit in text, (
            f"{module} no longer contains {lit}. This is on-disk format, not a "
            f"name. Changing it makes every vault written before now "
            f"undecryptable - see this module's docstring."
        )


def test_no_format_constant_carries_the_new_name():
    """The mirror of the above: catch the rewrite, not just the absence."""
    bad = []
    pattern = re.compile(r'b?"[^"]*compartment-(keyslot|record|pack|session|'
                         r'payload|journal|record-body)[^"]*"')
    for module in FROZEN:
        for m in pattern.finditer((SRC / module).read_text(encoding="utf-8")):
            bad.append(f"{module}: {m.group(0)}")
    assert not bad, (
        "on-disk constants were renamed to the new product name:\n  "
        + "\n  ".join(bad))


def test_keychain_identity_is_frozen():
    """The macOS Keychain item is a stored credential, not a label.

    Renaming the service or the account does not migrate the item - it makes
    the lookup miss, and the vault then reports itself locked with no hint
    that a perfectly good credential is sitting in the Keychain under the old
    name. That is what the rename did.
    """
    text = (SRC / "vault.py").read_text(encoding="utf-8")
    assert 'f"engram-vault:{os.path.abspath(path)}"' in text
    assert '"-a", "engram",' in text
    assert '"-a", "compartment",' not in text
    assert "compartment-vault:" not in text


def test_keyfile_separator_is_the_value_existing_keyfiles_used():
    from compartment.crypto import KEYFILE_SEP
    assert KEYFILE_SEP == b"\x1f engram-2fa \x1f"


def test_a_vault_written_now_still_round_trips():
    """Belt and braces: the constants agree with themselves."""
    from compartment.crypto import (make_passphrase_slot, open_slot,
                                    new_key, new_record_key, unwrap_record_key)
    master = new_key()
    slot = make_passphrase_slot(master, "correct horse")
    assert open_slot(slot, "correct horse") == master

    rk, wrapped = new_record_key(master, "rec-1")
    assert unwrap_record_key(master, "rec-1", wrapped) == rk
