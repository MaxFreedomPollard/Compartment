"""The LongMemEval harness, which had no cover at all.

A benchmark nobody tests is a benchmark that can be wrong for a long time
without anyone noticing, and this one was: a local named `evidence` shadowed
the imported scoring function, so every scored question raised "'set' object
is not callable" - after the whole embedding pass had already run. Five
defects are pinned here:

  1. the shadowed scorer (the crash)
  2. p95 taken with int(len*0.95), which returns the maximum for n <= 20
  3. document frequency measured by substring instead of FTS token match
  4. the keyword channel ORing every term, with no AND attempt and no
     common-term ceiling
  5. literal evidence handed to vector-only candidates, which the product
     scores at 0.0

None of this needs the dataset or the ONNX model: a stub embedder that returns
fixed vectors is enough to exercise the whole scoring path.
"""
import numpy as np
import pytest

from compartment import longmemeval as L
from compartment.ranking import CANDIDATE_POOL


DIM = 8


class StubEmbedder:
    """Deterministic stand-in. `dim` and the two embed calls are the whole
    surface _score_question touches once the cache is warm."""

    dim = DIM

    def __init__(self, qvec=None):
        self._qvec = qvec if qvec is not None else _unit(0)

    def embed_query(self, text):
        return self._qvec

    def embed_record(self, text):
        return np.atleast_2d(_unit(0))

    def chunk(self, text):
        return [text]


def _unit(i, dim=DIM):
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = 1.0
    return v


def _instance(sessions, answer_ids, question="where did I leave the keys?"):
    return {
        "question": question,
        "question_id": "q1",
        "question_type": "single-session-user",
        "haystack_sessions": sessions,
        "haystack_session_ids": [f"s{i}" for i in range(len(sessions))],
        "answer_session_ids": answer_ids,
    }


def _sessions(texts_per_session):
    return [[{"content": t} for t in texts] for texts in texts_per_session]


# -- 1. the crash ------------------------------------------------------------

def test_a_question_scores_instead_of_raising():
    """The regression that mattered: this raised TypeError for every question."""
    inst = _instance(_sessions([["the keys are on the hall table"],
                                ["we talked about the weather"]]), ["s0"])
    cache = {"the keys are on the hall table": np.atleast_2d(_unit(0)),
             "we talked about the weather": np.atleast_2d(_unit(1))}
    out = L._score_question(inst, StubEmbedder(_unit(0)), cache, (1, 5))
    assert out["skipped"] is False
    assert out["any@1"] is True
    assert out["all@1"] is True
    assert out["ms"] >= 0.0


def test_the_scorer_is_the_imported_function_not_a_local():
    code = L._score_question.__code__
    assert "evidence" not in code.co_varnames, \
        "a local named `evidence` shadows the imported scorer again"
    assert "evidence" in code.co_names


def test_a_wrong_session_is_not_credited():
    inst = _instance(_sessions([["something unrelated entirely"],
                                ["the keys are on the hall table"]]), ["s1"])
    cache = {"something unrelated entirely": np.atleast_2d(_unit(3)),
             "the keys are on the hall table": np.atleast_2d(_unit(0))}
    out = L._score_question(inst, StubEmbedder(_unit(3)), cache, (1,))
    assert out["any@1"] is False


# -- 2. percentiles ----------------------------------------------------------

def test_p95_of_twenty_samples_is_not_the_maximum():
    """int(len(xs) * 0.95) == 19 for n == 20, so the old form published the
    single slowest query as a p95."""
    lat = [float(i) for i in range(1, 21)]          # 1..20
    assert lat[int(len(lat) * 0.95)] == 20.0        # the defect
    assert L._pct(lat, 0.95) == 19.0                # nearest rank


def test_p50_takes_the_lower_median():
    assert L._pct([1.0, 2.0, 3.0, 4.0], 0.50) == 2.0


# -- 3. document frequency agrees with the weights it produces ---------------

def test_document_frequency_uses_token_match_not_substring():
    con = L._build_fts(["my keyboard is loud", "the key is missing"])
    try:
        # substring containment would answer 2 here; FTS tokenizes, so "key"
        # is not inside "keyboard".
        assert L._doc_frequency(con, "key") == 1
        assert L._doc_frequency(con, "keyboard") == 1
    finally:
        con.close()


def test_term_information_skips_terms_that_appear_nowhere():
    con = L._build_fts(["alpha beta", "beta gamma"])
    try:
        info = L._term_information(con, "alpha zebra", total=2)
        assert "alpha" in info and "zebra" not in info
    finally:
        con.close()


# -- 4. the keyword channel mirrors Store.fts_search -------------------------

def test_and_is_tried_before_or():
    """A turn containing every term must outrank one containing a single
    common term. Plain OR put them on equal footing."""
    texts = ["the report is about the budget",     # both terms
             "the budget of something else",       # one term
             "the report of something else"]       # one term
    con = L._build_fts(texts)
    try:
        ranks = L._fts_ranks(con, "report budget", CANDIDATE_POOL, len(texts))
        assert set(ranks) == {0}, "AND matched, so OR must not have run"
    finally:
        con.close()


def test_or_is_the_fallback_when_and_finds_nothing():
    texts = ["only the budget here", "only the report here"]
    con = L._build_fts(texts)
    try:
        ranks = L._fts_ranks(con, "budget report", CANDIDATE_POOL, len(texts))
        assert set(ranks) == {0, 1}
    finally:
        con.close()


def test_an_empty_query_matches_nothing_and_does_not_raise():
    con = L._build_fts(["anything at all"])
    try:
        assert L._fts_ranks(con, "   ", CANDIDATE_POOL, 1) == {}
        assert L._term_information(con, "   ", total=1) == {}
    finally:
        con.close()


# -- 5. literal evidence only for keyword hits -------------------------------

def test_a_vector_only_candidate_gets_no_literal_evidence():
    """The turn that shares no query term must score strictly below the one
    that does, even when both are equally close in vector space."""
    hit = "the keys are on the hall table"
    miss = "an entirely different sentence"
    inst = _instance(_sessions([[hit], [miss]]), ["s0"], question="keys table")
    # identical vectors: the only thing that can separate them is the lexical
    # channel, which must fire for the keyword hit and not for the other.
    cache = {hit: np.atleast_2d(_unit(0)), miss: np.atleast_2d(_unit(0))}
    out = L._score_question(inst, StubEmbedder(_unit(0)), cache, (1,))
    assert out["any@1"] is True


# -- windows -----------------------------------------------------------------

def test_a_turn_scores_by_its_best_window():
    """Turns are embedded as windows now. A turn whose SECOND window is the
    match must still win, which a single truncated vector per turn could not
    express."""
    long_turn = "opening that has nothing to do with it"
    other = "a different turn"
    inst = _instance(_sessions([[long_turn], [other]]), ["s0"])
    cache = {
        # window 0 points away from the query, window 1 points straight at it
        long_turn: np.vstack([_unit(5), _unit(0)]),
        other: np.atleast_2d(_unit(4)),
    }
    out = L._score_question(inst, StubEmbedder(_unit(0)), cache, (1,))
    assert out["any@1"] is True


def test_windows_map_back_to_their_own_turn():
    a, b = "first turn text", "second turn text"
    inst = _instance(_sessions([[a], [b]]), ["s1"])
    cache = {a: np.vstack([_unit(1), _unit(2), _unit(3)]),
             b: np.vstack([_unit(0), _unit(6)])}
    out = L._score_question(inst, StubEmbedder(_unit(0)), cache, (1,))
    assert out["any@1"] is True, "the 3-window turn stole the 2-window turn's hit"


# -- housekeeping ------------------------------------------------------------

def test_a_question_with_no_usable_turns_is_skipped():
    inst = _instance(_sessions([[""], ["   "]]), ["s0"])
    assert L._score_question(inst, StubEmbedder(), {}, (1,)) == {"skipped": True}


def test_missing_turns_are_embedded_rather_than_raising_keyerror():
    inst = _instance(_sessions([["a turn nobody cached"]]), ["s0"])
    out = L._score_question(inst, StubEmbedder(_unit(0)), {}, (1,))
    assert out["skipped"] is False


def test_the_reported_fusion_names_the_scorer_that_is_actually_used():
    """The result JSON used to advertise the additive RRF scorer, whose
    constants were deleted from this module."""
    import inspect
    src = inspect.getsource(L.run)
    assert "ranking.evidence()" in src
    assert "0.02*cosine" not in src
