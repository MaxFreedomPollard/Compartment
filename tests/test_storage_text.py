"""Text the encoder and SQLite cannot take as it arrives.

A JSON string may carry "\\ud83d" with no partner, and Python's decoder hands
it on as a lone surrogate. JavaScript clients that cut a string to length cut
emoji in half; Windows returns unpaired UTF-16 from file names and the
clipboard. Such a string has no UTF-8 encoding, so every layer below the
vault refuses it.
"""
import json

import pytest

from compartment.vault import Vault, clean_text

HALF_EMOJI = json.loads('"cut here \\ud83d"')        # what a JSON client sends
PAIR_APART = chr(0xD83D) + chr(0xDE00)            # a pair, as two points


def test_clean_text_replaces_only_what_cannot_be_encoded():
    assert clean_text("plain, ünïcode, 🦄 and עברית") == \
        "plain, ünïcode, 🦄 and עברית"
    assert clean_text(HALF_EMOJI) == "cut here \ufffd"
    assert clean_text(PAIR_APART) == "\N{GRINNING FACE}", "a split pair is joined, not lost"
    assert clean_text(["a\udc80b", "ok"]) == ["a\ufffdb", "ok"]
    assert clean_text(None) is None and clean_text(3) == 3


def test_a_memory_with_half_an_emoji_is_stored_and_found(vault):
    """It failed with "TextInputSequence must be str" from the tokenizer,
    and the memory was not stored."""
    text = "The deploy channel is #ops-alerts " + HALF_EMOJI
    out = vault.store(text, caller="t", source="chat \udc81",
                      tags=["ops\ud800"])
    got = vault.get(out["id"], caller="t")
    assert "\ufffd" in got["text"] and got["source"] == "chat \ufffd"
    assert got["tags"] == ["ops\ufffd"]
    hits = vault.search("deploy channel " + HALF_EMOJI, caller="t")
    assert [h["id"] for h in hits["results"]][:1] == [out["id"]]


def test_the_memory_survives_a_reopen(vault, vault_path):
    out = vault.store("Half an emoji at the end " + HALF_EMOJI, caller="t")
    again = Vault.unlock(vault_path, passphrase="CorrectHorse")
    assert again.get(out["id"], caller="t")["text"].startswith(
        "Half an emoji at the end cut here \ufffd")


def test_the_graph_takes_and_finds_the_same_text(vault):
    vault.link("Ana " + HALF_EMOJI, "works on", "the \udcffrelease", caller="t")
    rels = vault.relations("t", entity="Ana " + HALF_EMOJI)["relations"]
    assert [(r["subject"], r["object"]) for r in rels] == [
        ("Ana cut here \ufffd", "the \ufffdrelease")]
