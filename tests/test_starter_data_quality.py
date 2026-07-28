"""Data-quality guards on tools/starter/starter_facts.jsonl.

The JSONL is the canonical, hand-editable starter memory. These tests read it
directly (not the built pack) so a bad hand edit is caught before anyone runs
tools/build_starter_pack.py.
"""
import collections
import json
import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "tools" / "starter" / "starter_facts.jsonl")


def _records():
    return [json.loads(l) for l in
            SRC.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_every_line_is_valid_json_with_the_expected_schema():
    for n, line in enumerate(SRC.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"line {n} is not valid JSON: {exc}") from None
        assert set(rec) == {"id", "tags", "text"}, f"line {n} schema: {set(rec)}"
        assert isinstance(rec["id"], str) and rec["id"], f"line {n} bad id"
        assert isinstance(rec["text"], str) and rec["text"].strip(), \
            f"line {n} bad text"
        assert isinstance(rec["tags"], list) and \
            all(isinstance(t, str) for t in rec["tags"]), f"line {n} bad tags"


def test_ids_are_unique():
    ids = [r["id"] for r in _records()]
    dupes = [i for i, n in collections.Counter(ids).items() if n > 1]
    assert not dupes, f"duplicate ids: {dupes[:10]}"


def test_no_duplicate_texts():
    """Identical text under two ids wastes a slot and degrades top-k."""
    texts = collections.Counter(r["text"] for r in _records())
    dupes = [t for t, n in texts.items() if n > 1]
    assert not dupes, f"{len(dupes)} duplicated texts, e.g. {dupes[:3]}"


def test_frozen_selftest_corpus_intact():
    """core-001..core-260 are the frozen `compartment selftest` corpus."""
    core = sorted(r["id"] for r in _records() if r["id"].startswith("core-"))
    assert core == [f"core-{n:03d}" for n in range(1, 261)]


def test_selftest_expected_records_still_present():
    from compartment import selftest
    have = {r["id"] for r in _records()}
    missing = [want for _, want in selftest.QUERIES if want not in have]
    assert not missing, f"selftest expects missing ids: {missing}"


# Words that start with a vowel LETTER but a consonant SOUND, so "a" is
# correct in front of them. Compared lowercased.
CONSONANT_SOUND_VOWEL_WORDS = {
    "one", "once", "one-minute", "one-way", "one-time", "one-off",
    "unit", "units", "unitary", "unique", "universal", "universally",
    "universe", "university", "unified", "uniform", "union", "unicode",
    "unix", "user", "users", "usable", "useful", "usual", "umask",
    "uuid", "uri", "url", "url-safe", "urls", "usb", "usb-c", "usb-a",
    "utf-8", "utf-16", "utf-32", "us", "un", "un-monitored", "ui", "ux",
    "euro", "european", "eulogy", "ubiquitous",
}

_A_BEFORE_VOWEL = re.compile(r"\ba (?=[AaEeIiOoUu])([\w'-]+)")


def test_no_a_before_a_vowel_sound():
    bad = []
    for r in _records():
        for m in _A_BEFORE_VOWEL.finditer(r["text"]):
            if m.group(1).lower() not in CONSONANT_SOUND_VOWEL_WORDS:
                bad.append(f'{r["id"]}: "a {m.group(1)}"')
    assert not bad, f"{len(bad)} wrong indefinite articles, e.g. {bad[:8]}"


def test_no_broken_mass_of_portion_template():
    """"The mass of slice of a onions, raw ..." is generator breakage."""
    pat = re.compile(r"^The mass of .+ of an? .+ is typically about ")
    bad = [r["id"] for r in _records() if pat.match(r["text"])]
    assert not bad, f"{len(bad)} malformed 'mass of X of a Y' texts: {bad[:8]}"


def test_country_names_that_need_a_definite_article_have_one():
    needs_the = ("United Kingdom", "Netherlands", "Philippines",
                 "Czech Republic", "United States")
    bad = []
    for r in _records():
        for c in needs_the:
            if re.search(r"^The capital of %s\b" % re.escape(c), r["text"]):
                bad.append(f'{r["id"]}: {r["text"]}')
    assert not bad, f"missing definite article: {bad}"


def test_no_doubled_sentence_period():
    bad = [r["id"] for r in _records() if r["text"].endswith("..")
           and not r["text"].endswith("...")]
    assert not bad, f"doubled final period: {bad}"
