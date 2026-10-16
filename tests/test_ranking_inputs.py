"""What the scoring model is handed: text in whatever spelling it arrives in.

tests/test_ranking.py and tests/test_recency.py state the model itself and
are its contract; these hold its inputs to what the index beside it does.
"""
import re
import sqlite3

import pytest

from compartment import ranking as R

# The tokenizer as it was before accents were folded, for the pin below.
_ASCII_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _fts5_terms(text: str) -> list[str]:
    """The terms FTS5's default tokenizer, the one the store's keyword index
    uses, makes of `text`."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE fts USING fts5(text)")
        conn.execute("CREATE VIRTUAL TABLE terms USING fts5vocab(fts, 'row')")
        conn.execute("INSERT INTO fts (text) VALUES (?)", (text,))
        return sorted(r[0] for r in conn.execute("SELECT term FROM terms"))
    finally:
        conn.close()


@pytest.mark.parametrize("text", [
    "Zürich", "Zürich", "café", "Müller", "São Paulo", "résumé",
    "naïve", "Ångström", "Łódź", "İstanbul", "Crème brûlée"])
def test_an_accented_word_splits_into_the_terms_the_index_holds(text):
    """The index folds accents away, so each term's weight is counted over
    the folded word; coverage has to read the text the same way."""
    assert sorted(R._tokens(text)) == _fts5_terms(text)


def test_a_word_typed_without_its_accent_is_credited_and_the_reverse():
    assert R.information_coverage({"Zurich": 1.0},
                                  "The Zürich office keeps the badge") > 0.99
    assert R.information_coverage({"Zürich": 1.0},
                                  "The Zurich office keeps the badge") > 0.99
    assert R.information_coverage({"Zurich": 1.0},
                                  "The Zürich office") > 0.99


def test_text_without_accents_is_split_exactly_as_before():
    """The fold is the identity on ASCII, so no score of a text without
    accents moves: every printable character alone, between letters and
    against digits, and some sentences."""
    printable = [chr(c) for c in range(32, 127)]
    samples = (printable
               + [f"a{c}b" for c in printable]
               + [f"Key{c}9_x{c}" for c in printable]
               + ["Max decided to use Airtable for the ledger?",
                  "my keyboard is loud; the keychain is unlocked, sshd is off",
                  "port 5433 on db-1.internal (v2.4.1), see RFC_7231 & e-mail",
                  "  \t\n  ", ""])
    for s in samples:
        assert R._tokens(s) == _ASCII_TOKEN_RE.findall(s.lower()), repr(s)


def test_a_place_typed_without_its_accent_finds_the_memory_that_has_it(vault):
    """The keyword index found the Zürich memory for "Zurich", since FTS5
    folds the accent, and coverage then gave it no credit for the name, so
    four memories about other Swiss cities outranked it; on a 10,000-record
    vault it was not among the 24 results at all. A memory spelled without
    the accent had the same hole the other way round."""
    zurich = vault.store("The Zürich office keeps the spare badge in drawer 4.",
                         caller="test")["id"]
    malmo = vault.store("The Malmo depot keeps the forklift keys in locker 9.",
                        caller="test")["id"]
    for text in ("Switzerland is a country in central Europe.",
                 "Geneva hosts the European headquarters of the United Nations.",
                 "Basel sits on the Rhine where France, Germany and Switzerland meet.",
                 "Bern is the federal city of Switzerland.",
                 "Sweden is a country in northern Europe.",
                 "Gothenburg is the second largest city in Sweden.",
                 "Stockholm is the capital of Sweden."):
        vault.store(text, caller="test")
    for rid, spellings in ((zurich, ("Zurich", "Zürich")),
                           (malmo, ("Malmö", "Malmo"))):
        scores = []
        for q in spellings:
            top = vault.search(q, caller="test")["results"][0]
            assert top["id"] == rid, (q, top["text"])
            scores.append(top["score"])
        assert scores[0] == pytest.approx(scores[1])
