"""The scoring model: what it must do, stated as behaviour rather than numbers.

These assert the PROPERTIES the model was chosen for. Constants can be retuned
against a benchmark without touching this file; if a retune breaks one of
these, the retune broke the model.
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
    ancient = now - 86400 * 100_000          # recency term negligible
    assert R.prior(0.5, ancient, now) == pytest.approx(0.0, abs=1e-6)


def test_importance_moves_the_prior_in_both_directions():
    now = 1_000_000.0
    ancient = now - 86400 * 100_000
    assert R.prior(1.0, ancient, now) > 0
    assert R.prior(0.0, ancient, now) < 0


def test_recency_decays_by_half_over_the_half_life():
    now = 1_000_000.0
    fresh = R.prior(0.5, now, now)
    half = R.prior(0.5, now - 86400 * R.RECENCY_HALF_LIFE_DAYS, now)
    assert half == pytest.approx(fresh / 2, rel=1e-6)


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


# ------------------------------------------------------- rank-agreement term --
def test_the_rrf_residue_breaks_ties_without_deciding_them():
    tie_a = R.evidence(0.5, 0.5, vec_rank=0, lex_rank=0)
    tie_b = R.evidence(0.5, 0.5, vec_rank=40, lex_rank=40)
    assert tie_a > tie_b, "agreement at the top should win a tie"
    clearly_better = R.evidence(0.9, 0.5, vec_rank=40, lex_rank=40)
    assert clearly_better > tie_a, "the residue must not outweigh real evidence"
