"""What the scoring model is handed: text in whatever spelling it arrives in,
and importance on whatever scale.

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


# --------------------------------------------------- importance off its scale
# Only the MCP server clamped importance to 0..1. The CLI's --importance, the
# Hermes plugin, imports and the Python API passed it through, and the prior
# is built for 0..1: 50 multiplied a score by 15.85, -3 made it negative.

def test_an_importance_past_either_end_weighs_what_that_end_weighs():
    now = 1_000_000.0
    for kind in ("fact", "opinion"):
        top = R.prior(1.0, now, now, kind=kind)
        bottom = R.prior(0.0, now, now, kind=kind)
        assert R.prior(50, now, now, kind=kind) == top
        assert R.prior(8.0, now, now, kind=kind) == top
        assert R.prior(-3, now, now, kind=kind) == bottom
    assert R.final_score(0.6, 0.0, importance=-3, created=now, now=now) > 0


def test_an_importance_in_range_scores_exactly_as_before():
    now = 1_000_000.0
    for imp in [i / 40 for i in range(41)] + [1e-12, 0.123456789, 1 - 1e-12]:
        assert R.prior(imp, now, now) == R.W_IMPORTANCE * (2.0 * imp - 1.0)


def _store_beside_two(vault, text, importance):
    right = vault.store("The staging server runs Postgres 15 on port 5433.",
                        caller="test")["id"]
    vault.store("Backups run nightly at 02:00 to the NAS.", caller="test")
    vault.store("The office printer is on the second floor.", caller="test")
    other = vault.store(text, caller="test", importance=importance)["id"]
    return right, other


def test_a_memory_stored_at_importance_50_does_not_answer_other_questions(
        vault):
    right, loud = _store_beside_two(
        vault, "The office router reboots every Sunday.", 50)
    assert vault.get(loud, caller="test")["importance"] == 1.0
    top = vault.search("staging Postgres port", caller="test")["results"][0]
    assert top["id"] == right


def test_a_memory_stored_at_importance_8_admits_no_wrong_answer(vault):
    """A 1-10 scale is what models and other tools often send."""
    right, wrong = _store_beside_two(
        vault, "The production database listens on port 5432.", 8.0)
    found = [r["id"] for r in vault.search(
        "which port does the staging Postgres use", caller="test")["results"]]
    assert found[0] == right and wrong not in found


def test_a_memory_stored_at_importance_minus_3_is_still_found(vault):
    _, low = _store_beside_two(
        vault, "The office router reboots every Sunday.", -3)
    assert vault.get(low, caller="test")["importance"] == 0.0
    found = vault.search("when does the router reboot", caller="test")
    assert [r["id"] for r in found["results"]][:1] == [low]


def test_a_nan_importance_is_stored_as_the_default(vault):
    rid = vault.store("The office router reboots every Sunday.",
                      caller="test", importance=float("nan"))["id"]
    assert vault.get(rid, caller="test")["importance"] == 0.5


def test_an_importance_stored_before_the_clamp_is_ranked_as_if_clamped(vault):
    """What earlier versions wrote is still in the column."""
    right, _ = _store_beside_two(
        vault, "Invoices are due on the fifth of each month.", 0.5)
    vec = vault.embedder.embed_record("The office router reboots every Sunday.")
    vault.db.insert(record_id=None, ns="main",
                    text="The office router reboots every Sunday.", vec=vec,
                    tags=[], importance=50.0, quarantined=False, pack=None,
                    prov={}, master_key=vault._master)
    vault._rebuild_index()
    top = vault.search("staging Postgres port", caller="test")["results"][0]
    assert top["id"] == right
