"""The scoring model: what it must do, stated as behaviour rather than numbers.

These assert the PROPERTIES the model was chosen for. Constants can be retuned
against a benchmark without touching this file; if a retune breaks one of
these, the retune broke the model.

Changing the model itself is the other thing, and that does move tests. A
fact's recency is now counted in memories rather than in days, so the two
assertions about a wall-clock half-life were replaced by the count-based ones
below rather than adjusted to fit.
"""
import math

import pytest

from compartment import ranking as R


# ------------------------------------------------- reading a cosine as a p ---
def test_cosine_below_the_floor_is_no_evidence():
    assert R.p_from_cosine(R.COS_FLOOR) == 0.0
    assert R.p_from_cosine(0.0) == 0.0
    assert R.p_from_cosine(-0.5) == 0.0


def test_cosine_above_the_ceiling_saturates_but_stays_finite():
    p = R.p_from_cosine(1.0)
    assert p < 1.0, "p must stay below 1 or -log(1-p) is infinite"
    assert R.p_from_cosine(R.COS_CEIL) == pytest.approx(p, abs=1e-9)


def test_a_semantic_match_can_never_reach_the_certainty_of_a_literal_one():
    """A cosine says "about the same thing", never "this exact record"."""
    assert R.p_from_cosine(1.0) <= R.VEC_CERTAINTY_CAP
    assert R.VEC_CERTAINTY_CAP < 0.90, (
        "above 0.90 a saturated cosine outweighs conclusive literal evidence")


def test_cosine_maps_monotonically_between_the_bounds():
    mid = (R.COS_FLOOR + R.COS_CEIL) / 2
    assert 0.0 < R.p_from_cosine(mid) < R.p_from_cosine(R.COS_CEIL)


def test_a_missing_cosine_is_no_evidence_rather_than_an_error():
    assert R.p_from_cosine(None) == 0.0


# ---------------------------------------------------- information coverage ---
def test_coverage_is_the_share_of_query_information_explained():
    info = {"alpha": 1.0, "beta": 3.0}
    assert R.information_coverage(info, "alpha only") == pytest.approx(0.25)
    assert R.information_coverage(info, "beta only") == pytest.approx(0.75)
    assert R.information_coverage(info, "alpha and beta") > 0.99


def test_coverage_ignores_case():
    assert R.information_coverage({"Commit": 2.0}, "the COMMIT landed") > 0.99


def test_matching_nothing_is_zero_coverage():
    assert R.information_coverage({"zebra": 2.0}, "nothing here") == 0.0


def test_a_query_with_no_information_cannot_divide_by_zero():
    assert R.information_coverage({}, "anything") == 0.0


# -------------------------------------------------------- combining channels --
def test_either_channel_alone_can_carry_a_memory():
    """The property the old additive scorer did not have."""
    literal_only = R.evidence(p_vec=0.0, p_lex=0.99)
    semantic_only = R.evidence(p_vec=0.99, p_lex=0.0)
    assert literal_only > 0
    assert semantic_only > 0


def test_a_decisive_literal_match_outranks_a_strong_semantic_one():
    """A commit sha found in exactly one memory must beat a paraphrase.

    This is the failure that motivated the model: under the previous additive
    fusion the paraphrase won, because a weighted sum cannot let one channel
    be conclusive.
    """
    exact = R.evidence(p_vec=R.p_from_cosine(0.45), p_lex=0.999)
    paraphrase = R.evidence(p_vec=R.p_from_cosine(0.85), p_lex=0.0)
    assert exact > paraphrase


def test_neither_channel_can_veto_the_other():
    both = R.evidence(p_vec=0.9, p_lex=0.9)
    one = R.evidence(p_vec=0.9, p_lex=0.0)
    assert both > one, "agreement must never score below one channel alone"


def test_evidence_is_monotone_in_each_channel():
    base = R.evidence(0.5, 0.5)
    assert R.evidence(0.6, 0.5) > base
    assert R.evidence(0.5, 0.6) > base


def test_no_evidence_scores_zero():
    assert R.evidence(0.0, 0.0) == pytest.approx(0.0)


def test_evidence_ranks_the_same_as_the_noisy_or_it_comes_from():
    """The score is a monotone transform of 1 - (1-pv)(1-pl), which is the
    whole justification for using it. Compare orderings PAIRWISE rather than by
    sorting: the two formulas tie in different floating-point directions, so a
    sorted list would differ on ties while the ordering is in fact identical."""
    def noisy(p):
        return 1 - (1 - p[0]) * (1 - p[1])

    def equal_weight(p):
        return -math.log(1 - p[0]) - math.log(1 - p[1])

    pairs = [(a / 10, b / 10) for a in range(10) for b in range(10)]
    for x in pairs:
        for y in pairs:
            if noisy(x) > noisy(y) + 1e-12:
                assert equal_weight(x) > equal_weight(y)


# ------------------------------------------------------------------ priors ---
def test_the_default_importance_is_exactly_neutral():
    """0.5 is what every unweighted memory carries, including thousands of
    starter facts. If it were not neutral they would all drift together."""
    now = 1_000_000.0
    assert R.prior(0.5, now, now) == pytest.approx(0.0, abs=1e-6)


def test_importance_moves_the_prior_in_both_directions():
    now = 1_000_000.0
    ancient = now - 86400 * 100_000
    assert R.prior(1.0, ancient, now) > 0
    assert R.prior(0.0, ancient, now) < 0


def test_a_fact_carries_no_recency_in_its_prior():
    """A fact's age is counted in memories, by the search that knows how many
    the vault holds, so there is nothing about time left in prior()."""
    now = 1_000_000.0
    fresh = R.prior(0.5, now, now)
    ancient = R.prior(0.5, now - 86400 * 100_000, now)
    assert fresh == ancient == 0.0


def test_priors_are_multiplicative_so_they_cannot_invent_a_match():
    """An unmatched memory scores zero, and no prior can lift it off zero."""
    unmatched = R.final_score(p_vec=0.0, p_lex=0.0, importance=1.0,
                              created=1_000_000.0, now=1_000_000.0)
    assert unmatched == pytest.approx(0.0)


def test_importance_reranks_a_genuine_tie():
    now = 1_000_000.0
    important = R.final_score(0.6, 0.0, importance=0.95, created=now, now=now)
    ordinary = R.final_score(0.6, 0.0, importance=0.50, created=now, now=now)
    assert important > ordinary


def test_importance_cannot_overturn_a_clearly_better_match():
    now = 1_000_000.0
    better = R.final_score(0.90, 0.0, importance=0.0, created=now, now=now)
    weaker = R.final_score(0.20, 0.0, importance=1.0, created=now, now=now)
    assert better > weaker


def test_a_prior_never_flips_the_sign_of_a_score():
    now = 1_000_000.0
    for imp in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert R.final_score(0.5, 0.5, imp, now, now=now) > 0


# ------------------------------------------ recency, counted in memories ---
# A memory's age is the SHARE of the vault written after it. The prior enters
# in odds, and it shifts the semantic term only.

def test_the_newest_memory_keeps_all_of_its_odds():
    assert R.recency_odds(0.0) == 1.0


def test_a_memory_outside_the_population_carries_no_recency_prior():
    """A pack record and a starting memory arrived all at once, so they have
    no age in this sense and the vault hands them no share at all."""
    assert R.recency_odds(None) == 1.0


def test_the_odds_halve_when_the_half_life_share_of_the_vault_is_newer():
    """The count-based reading of the old 180-day half-life: the unit is how
    much of the vault came after, not how long ago it was written."""
    full = R.recency_odds(0.0)
    half = R.recency_odds(R.RECENCY_HALF_LIFE_SHARE)
    quarter = R.recency_odds(2 * R.RECENCY_HALF_LIFE_SHARE)
    assert half == pytest.approx(full / 2)
    assert quarter == pytest.approx(half / 2)


def test_the_oldest_memory_in_the_vault_keeps_a_quarter_of_its_odds():
    assert R.recency_odds(1.0) == pytest.approx(0.25)


def test_a_shorter_half_life_share_ages_a_memory_faster():
    assert R.recency_odds(0.5, half_life_share=0.25) == pytest.approx(0.25)
    assert R.recency_odds(0.5, half_life_share=1.0) > R.recency_odds(0.5)


def test_a_half_life_share_of_zero_turns_the_recency_prior_off():
    assert R.recency_odds(1.0, half_life_share=0.0) == 1.0
    assert R.aged_term(0.8, 1.0, half_life_share=0.0) == 0.8


def test_a_newer_share_outside_its_own_range_is_clamped():
    assert R.recency_odds(-3.0) == 1.0
    assert R.recency_odds(7.0) == pytest.approx(R.recency_odds(1.0))


def test_the_odds_fall_as_more_of_the_vault_is_newer():
    odds = [R.recency_odds(i / 20) for i in range(21)]
    assert all(b < a for a, b in zip(odds, odds[1:]))


def test_ageing_the_newest_memory_changes_nothing():
    for s in (0.01, 0.4, 1.59):
        assert R.aged_term(s, 0.0) == s
        assert R.aged_term(s, None) == s


def test_no_evidence_stays_no_evidence_however_old_it_is():
    """Recency reorders matches; it cannot lift a memory off zero and cannot
    push a matched one down to it."""
    assert R.aged_term(0.0, 1.0) == 0.0
    assert R.aged_evidence(0.6, 0.0, 1.0) > 0.0


def test_ageing_can_only_take_evidence_away():
    for q in (0.1, 0.5, 0.9, 1.0):
        for s in (0.05, 0.4, 1.59):
            assert R.aged_term(s, q) <= s


def test_ageing_keeps_the_order_of_equally_old_memories():
    """Monotone in the evidence, so the prior reorders memories of DIFFERENT
    ages and can never shuffle two of the same age."""
    aged = [R.aged_term(i / 100, 0.5) for i in range(1, 160)]
    assert all(b > a for a, b in zip(aged, aged[1:]))


def test_strong_evidence_is_nudged_rather_than_cut():
    """The reason the shift is applied in odds. Even the oldest memory in the
    vault loses at most log 4 nats off a saturated semantic match."""
    s = R.evidence(R.p_from_cosine(1.0), 0.0)
    assert R.aged_term(s, 1.0) >= s + math.log(0.25)
    assert R.aged_term(s, 1.0) > 0.4 * s


def test_weak_evidence_is_cut_in_proportion_to_the_odds():
    """The other half of that: a faint match is scaled, not nudged."""
    for q in (0.25, 0.5, 1.0):
        assert R.aged_term(0.01, q) == pytest.approx(R.recency_odds(q) * 0.01,
                                                     rel=0.02)


def test_a_two_week_old_memory_in_a_quiet_vault_keeps_its_evidence():
    """The request, first half: two memories written out of a thousand since,
    so the vault has not moved on and neither has the answer."""
    p = R.p_from_cosine(0.65)
    assert R.aged_evidence(p, 0.0, 2 / 1000) >= 0.99 * R.evidence(p, 0.0)


def test_a_memory_with_half_the_vault_newer_loses_most_of_its_evidence():
    """The request, second half: the same fortnight, five hundred memories
    written out of a thousand since."""
    p = R.p_from_cosine(0.65)
    assert R.aged_evidence(p, 0.0, 0.5) <= 0.65 * R.evidence(p, 0.0)


def test_among_equal_evidence_the_newer_memory_always_wins():
    p = R.p_from_cosine(0.7)
    scored = [R.aged_evidence(p, 0.0, q)
              for q in (0.0, 0.05, 0.2, 0.5, 0.8, 1.0)]
    assert all(b < a for a, b in zip(scored, scored[1:]))


def test_recency_shifts_the_semantic_channel_and_leaves_the_literal_one():
    """What a question MEANS is a guess that age can weaken. What it NAMES it
    cannot: an identifier occurring in one memory names that memory."""
    assert R.aged_evidence(0.0, 0.9, 1.0) == pytest.approx(R.evidence(0.0, 0.9))
    assert R.aged_evidence(0.9, 0.0, 1.0) < R.evidence(0.9, 0.0)


def test_the_literal_bound_survives_age():
    """The property the whole model exists for, under the new prior: the
    oldest memory in the vault, holding the sha that was typed, still beats
    the freshest paraphrase."""
    exact = R.final_score(p_vec=0.0, p_lex=0.999, importance=0.5,
                          created=0.0, now=0.0, newer_share=1.0)
    paraphrase = R.final_score(p_vec=0.88, p_lex=0.0, importance=0.5,
                               created=0.0, now=0.0, newer_share=0.0)
    assert exact > paraphrase


def test_a_pack_record_scores_exactly_its_unaged_evidence():
    aged = R.final_score(0.7, 0.2, 0.5, 0.0, now=0.0, newer_share=None)
    plain = R.final_score(0.7, 0.2, 0.5, 0.0, now=0.0)
    assert aged == plain == pytest.approx(R.evidence(0.7, 0.2))


# ------------------------------------------------------- rank-agreement term --
def test_the_rrf_residue_breaks_ties_without_deciding_them():
    tie_a = R.evidence(0.5, 0.5, vec_rank=0, lex_rank=0)
    tie_b = R.evidence(0.5, 0.5, vec_rank=40, lex_rank=40)
    assert tie_a > tie_b, "agreement at the top should win a tie"
    clearly_better = R.evidence(0.9, 0.5, vec_rank=40, lex_rank=40)
    assert clearly_better > tie_a, "the residue must not outweigh real evidence"


# ------------------------------------- matching the index that weighs it ---
# information_coverage used a raw `in` test while the document frequency
# behind every weight is measured by FTS5, which splits on non-alphanumerics.
# The two disagreed in both directions.

def test_a_trailing_question_mark_does_not_destroy_coverage():
    """FTS counts "Airtable?" as `airtable` in the denominator, so a raw
    substring test never credited it: ending a question with "?" halved the
    lexical evidence for a perfect match."""
    text = "Max decided to use Airtable for the ledger"
    clean = R.information_coverage({"Max": 4.6, "Airtable": 4.6}, text)
    asked = R.information_coverage({"Max": 4.6, "Airtable?": 4.6}, text)
    assert asked == pytest.approx(clean)
    assert asked > 0.99


def test_punctuation_around_a_term_is_ignored():
    for term in ("keys,", "(keys)", "keys.", '"keys"', "keys!"):
        assert R.information_coverage({term: 1.0}, "the keys are here") > 0.99


def test_a_term_is_not_credited_for_matching_inside_a_longer_word():
    """"key" scored full conclusive-literal-evidence against "keyboard", so a
    search for an SSH key was handed every memory mentioning a keychain."""
    assert R.information_coverage({"key": 1.0}, "my keyboard is loud") == 0.0
    assert R.information_coverage({"ssh": 2.0, "key": 3.0},
                                  "the keychain is unlocked, sshd is off") == 0.0


def test_a_multi_token_term_needs_all_of_its_tokens():
    assert R.information_coverage({"hall-table": 1.0},
                                  "the hall table is bare") > 0.99
    assert R.information_coverage({"hall-table": 1.0},
                                  "the hall is bare") == 0.0


def test_matching_is_still_case_insensitive():
    assert R.information_coverage({"Commit": 2.0}, "the COMMIT landed") > 0.99
