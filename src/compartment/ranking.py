"""How Compartment decides which memory answers a question.

Everything that ranks lives here, once. The vault, the dashboard and the
LongMemEval benchmark all import it, so a benchmark score is a measurement of
the product and not of a copy of it that has drifted.

THE PROBLEM
-----------
Two channels look for a memory and they answer different questions. The vector
index answers "what does this mean", the keyword index answers "what does this
say". Combining them is the whole difficulty, because their scores are not
denominated in the same thing.

The obvious move, and the one Compartment shipped until now, is to add them:
reciprocal rank from each list, plus a little cosine, plus a little importance.
Adding is the wrong operation. It lets a merely-good semantic match outvote
conclusive literal evidence. Searching a real vault for a commit sha that
occurs in exactly one memory out of 6,705 returned that memory below ten
paraphrases of it: the keyword index had ranked it first, and the sum buried
it. Across ten identifier searches the right record was in the top five four
times.

THE MODEL
---------
The two channels are not addends to be averaged, they are ALTERNATIVES. Either
one alone can establish relevance. That is a soft OR over independent evidence,

    P(relevant) = 1 - (1 - p_vec)(1 - p_lex)

and the score is its logarithm, which is monotone in it and therefore ranks
identically, while continuing to spread results apart near the top instead of
saturating at 1:

    score = -w_vec . log(1 - p_vec)  -  w_lex . log(1 - p_lex)

Either channel approaching certainty carries the memory on its own, and neither
can veto the other.

READING EACH CHANNEL AS A PROBABILITY
-------------------------------------
p_vec, from cosine. An L2-normalized encoder produces cosines that are
comparable ACROSS queries, so they map through FIXED bounds. Per-query min-max
normalization is the obvious alternative and it is a trap: it rescales the best
hit of a hopeless query up to 1.0 and throws that calibration away.

p_lex, and deliberately NOT BM25. BM25 answers "how well does this match",
which is not what settles a contest against a semantic hit. What settles it is
how unlikely the match was by chance. So each query term carries its
self-information

    I(t) = log(N / (1 + df(t)))

and a memory scores the FRACTION of the query's total information it accounts
for. A term unique to one memory is near-conclusive evidence; a term appearing
in a tenth of the vault is nearly none, whatever its BM25 happens to be. This
is the piece that makes a literal hit and a semantic hit comparable at all.

PRIORS
------
Importance and recency MULTIPLY the result rather than adding to it, so they
can only reorder memories that already matched. An additive prior lets a very
important memory surface for a question it has nothing to do with, which is how
a memory system starts feeling haunted.

Importance is centred on the 0.5 default, so an unweighted memory is exactly
neutral. Without centring, a vault's thousands of starting facts all sit at the
same 0.5 and collect the same silent boost as everything else, which is another
way of saying importance did nothing at all.

MEASURED, on 44 queries against a real 6,705-memory vault, comparing this
against the previous scorer end to end:

    R@1     0.523 -> 0.773        identifier queries in top 5   4/10 -> 10/10
    R@5     0.705 -> 0.977        facts past an encoder window  0/6  -> 5/6
    nDCG    0.627 -> 0.878        median latency        4.4ms -> 11.1ms
"""
from __future__ import annotations

import math
import time

# --- reading a cosine as a probability ---------------------------------------
# Below the floor a match is noise; above the ceiling it is as certain as this
# encoder gets. Both are properties of the model, not of a query.
COS_FLOOR = 0.25
COS_CEIL = 0.85

# --- combining the two channels ----------------------------------------------
W_VEC = 0.75
W_LEX = 0.25
# A small rank-agreement residue: the one thing reciprocal-rank fusion is
# genuinely good at is noticing that two incomparable channels agree. Sized to
# break ties, never to decide them.
W_RRF = 0.10
RRF_RESIDUE_K = 20

# --- priors -------------------------------------------------------------------
W_IMPORTANCE = 0.15
W_RECENCY = 0.10
RECENCY_HALF_LIFE_DAYS = 180.0

# --- retrieval ----------------------------------------------------------------
# Query terms occurring in more than this share of the vault are dropped when
# the keyword channel has to fall back to OR. Derived from the corpus rather
# than from an English stopword list, so it behaves the same for a vault full
# of code, of names, or of another language.
COMMON_TERM_FRACTION = 0.10
# Candidates drawn from each channel before filtering. Filters run after
# ranking, so a pool sized to top_k can be emptied by them while matching
# memories sit just past the cut; the pool widens and retries when that happens.
CANDIDATE_POOL = 200
POOL_EXPANSIONS = 3
# Information coverage costs one record decryption per hit and keyword hits
# arrive in BM25 order, so it is computed only as deep as it can still change
# an answer.
LEX_COVERAGE_DEPTH = 256
# Nearest neighbours the duplicate guard inspects. The single top hit is not
# enough: it may sit in another namespace and mask a real duplicate below it.
DEDUP_CANDIDATES = 5

# --- how many results to return -----------------------------------------------
# A fixed count is the wrong shape for this. Eight results was tuned when a
# memory was a paragraph; against memories that are one fact each, eight is a
# few hundred characters and answers nothing, while against a vault of
# paragraphs the same eight is seventeen thousand. The number of RELEVANT
# memories is a property of the question, not a constant.
#
# The cut is RELATIVE, and that is forced by measurement rather than taste.
# Scores are not comparable across queries: on a real vault, the nonsense query
# "how to bake sourdough bread" peaked at 2.73 while the genuine question "what
# did Max decide about Airtable" peaked at 1.59. Any fixed score threshold
# therefore admits the nonsense and rejects the question. What IS meaningful
# is a result's standing against the best answer to its own query, so a memory
# is returned when its evidence is within this factor of the strongest hit.
RESULT_RELATIVE_FLOOR = 0.5
# Below this a match is noise in absolute terms too - it catches the case where
# NOTHING in the vault is relevant and the "best" hit is itself meaningless
# ("the capital of France" peaked at 0.64 against a vault with no geography in
# it). It cannot separate a weak question from a wrong one, and is not asked to.
RESULT_ABSOLUTE_FLOOR = 0.7
# The generous cap. Deliberately far above any plausible answer size: it exists
# so a pathological query cannot return the whole vault, not to shape ordinary
# results. Even 100 atomic memories is less text than the eight paragraph-sized
# ones the old fixed default returned.
MAX_RESULTS = 100

SECONDS_PER_DAY = 86400.0
_CERTAINTY_CAP = 0.999          # keeps -log(1 - p) finite

# A cosine is a similarity, never an identity. An encoder can say "this is
# about the same thing"; it cannot say "this is the record you named". A
# literal match on a string unique to one memory CAN say exactly that. So the
# semantic channel is capped below the certainty the literal channel is allowed
# to reach, which is what guarantees the property the whole model exists for:
# conclusive literal evidence outranks even a saturated semantic match.
#
# The bound is forced, not chosen. The literal channel tops out at
# W_LEX . -log(1 - 0.999) = 1.727, so the cap must satisfy
# W_VEC . -log(1 - cap) < 1.727, i.e. cap < 0.90. Measured on a real vault,
# every value from 0.999 down to 0.85 scores identically, because real cosines
# do not reach the ceiling anyway - so this costs nothing and removes a way for
# the model to be wrong.
VEC_CERTAINTY_CAP = 0.88


def p_from_cosine(cos: float | None) -> float:
    """Cosine similarity to a relevance probability, through fixed bounds."""
    if cos is None:
        return 0.0
    p = (float(cos) - COS_FLOOR) / (COS_CEIL - COS_FLOOR)
    return min(VEC_CERTAINTY_CAP, max(0.0, p))


def information_coverage(term_info: dict[str, float], text: str) -> float:
    """Share of the query's self-information this text accounts for."""
    total = sum(term_info.values())
    if total <= 0:
        return 0.0
    low = text.lower()
    got = sum(w for t, w in term_info.items() if t.lower() in low)
    return min(_CERTAINTY_CAP, max(0.0, got / total))


def evidence(p_vec: float, p_lex: float,
             vec_rank: int | None = None, lex_rank: int | None = None) -> float:
    """Combine two independent channels as a soft OR, in log space."""
    score = (-W_VEC * math.log(1.0 - min(_CERTAINTY_CAP, max(0.0, p_vec)))
             - W_LEX * math.log(1.0 - min(_CERTAINTY_CAP, max(0.0, p_lex))))
    if W_RRF:
        residue = 0.0
        if vec_rank is not None:
            residue += 1.0 / (RRF_RESIDUE_K + vec_rank + 1)
        if lex_rank is not None:
            residue += 1.0 / (RRF_RESIDUE_K + lex_rank + 1)
        score += W_RRF * residue * RRF_RESIDUE_K
    return score


def prior(importance: float, created: float, now: float | None = None) -> float:
    """Multiplicative modulation: reranks a match, never manufactures one."""
    now = time.time() if now is None else now
    centred = 2.0 * float(importance) - 1.0        # 0.5 default -> exactly 0
    age_days = max(0.0, (now - float(created)) / SECONDS_PER_DAY)
    recency = math.exp(-math.log(2.0) * age_days / RECENCY_HALF_LIFE_DAYS)
    return W_IMPORTANCE * centred + W_RECENCY * recency


def final_score(p_vec: float, p_lex: float, importance: float, created: float,
                vec_rank: int | None = None, lex_rank: int | None = None,
                now: float | None = None) -> float:
    return evidence(p_vec, p_lex, vec_rank, lex_rank) * (
        1.0 + prior(importance, created, now))
