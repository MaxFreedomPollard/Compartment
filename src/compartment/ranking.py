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
Importance MULTIPLIES the result rather than adding to it, so it can only
reorder memories that already matched. An additive prior lets a very important
memory surface for a question it has nothing to do with, which is how a memory
system starts feeling haunted.

Importance is centred on the 0.5 default, so an unweighted memory is exactly
neutral. Without centring, a vault's thousands of starting facts all sit at the
same 0.5 and collect the same silent boost as everything else, which is another
way of saying importance did nothing at all.

Opinions carry a second, deliberate prior on top of that: a stance decays on a
short wall-clock half-life from the day it was last re-affirmed, so among live
opinions on one subject the newest wins. That one is about a claim that
REVISES rather than accumulates, and it is separate from the recency below.

The net doctrine: importance and recency reorder matches. Neither can invent
one, and recency cannot delete one either.

RECENCY, COUNTED IN MEMORIES RATHER THAN DAYS
---------------------------------------------
A fact does not get less true in six months. What makes an old memory the wrong
answer is that the vault has moved on since, and how far it has moved is a
question about how much was written, not about how long it took. A vault that
gained two memories in a fortnight has not moved on; a vault that gained five
hundred has. So a memory's age here is the SHARE of the vault that is newer
than it,

    q(m) = |{ r in P : t(r) > t(m) }| / |P|

with t = affirmed if set, else created, so a re-affirmed memory is a recent
memory. The newest record has q = 0, the median one q = 0.5, the oldest one
q just under 1.

P, the population, is the LIVE ORGANIC records of the namespaces being
searched. Not the starting memories: every install seeds thousands of them at
one instant, and counting them would park a user's first few hundred own
memories at q below 0.07, leaving the prior inert for exactly the people the
product ships to. A pack arrived all at once and has no age in this sense, so
a pack record carries no recency prior at all (q = None). Namespace-scoped
because in a shared vault one agent writing five thousand records in a week
must not push another agent's whole history to q = 1.

The prior enters in ODDS, not as a multiplier on the score. An evidence term s
is -log P(not relevant), so its odds are e^s - 1, a prior multiplies odds, and
the shifted term is log(1 + rho . (e^s - 1)). Doing it in odds is what makes
the shift behave: strong evidence is NUDGED (it loses at most log 4 nats, even
at q = 1) while weak evidence is CUT in proportion, which is the right way
round. A multiplier on the score would take the same fraction off both.

It shifts the SEMANTIC term only. Recency is a prior on which memory a
question MEANS. A literal identifier says which memory a question NAMES, and
naming does not age: the record holding the one commit sha the user typed is
the answer whether it was written yesterday or last year. Leaving the literal
channel alone also keeps the forced bound above intact under the new prior,
since the semantic term can now only go down.

    cosine  semantic term   q=0     q=0.1   q=0.5   q=1.0
    0.50    0.404           1.00    0.89    0.55    0.29
    0.65    0.824           1.00    0.91    0.60    0.34
    0.80    1.590           1.00    0.93    0.68    0.43

MEMBERSHIP IS DECIDED WITHOUT RECENCY. The floors that choose how many results
come back read the UNAGED evidence; recency then orders what was admitted. The
absolute floor asks whether the evidence is real, which is not a question age
can answer, and without this rule a vault's only relevant memory would drop
below the floor for being old and the vault would answer "nothing found" about
something it knows.

Cost: one SQL scan per mutation to rebuild the population (about 10 ms at
7,000 records), then a masked binary search per candidate, well under a
millisecond per query.

MEASURED, on 44 queries against a real 6,705-memory vault, comparing this
against the previous scorer end to end:

    R@1     0.523 -> 0.773        identifier queries in top 5   4/10 -> 10/10
    R@5     0.705 -> 0.977        facts past an encoder window  0/6  -> 5/6
    nDCG    0.627 -> 0.878        median latency        4.4ms -> 11.1ms
"""
from __future__ import annotations

import math
import re
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
# How much of the vault has to be newer than a memory before its semantic
# evidence is worth half the odds. 0.5 says the median memory, the one with
# half the vault written after it, is the half-way point: the newest memory
# keeps odds 1, the oldest keeps 1/4. A smaller share ages memories faster and
# starts hiding a vault's older half behind whatever was written last month; a
# larger one flattens the prior until a busy vault stops preferring what it
# just learned. 0.5 is also the only value with a plain reading, which is what
# a user editing the setting has to reason with.
RECENCY_HALF_LIFE_SHARE = 0.5
# Opinions age differently from facts, and on a different clock. A fact
# learned in March is as true in September, and what dates it is the vault
# having moved on, which is the count above. A stance held in March may have
# been revised twice since, and what a reader wants is the CURRENT one, which
# is a question about the calendar however quiet the vault has been. So an
# opinion carries this SECOND prior on top: it runs from the day the stance
# was last re-affirmed (falling back to created), decays on a short wall-clock
# half-life, and is weighted heavily enough that among live opinions on one
# subject the newest wins decisively, while staying multiplicative: it
# reorders matches and can never manufacture one. The
# stronger mechanism for replaced opinions is superseding, which removes the
# old record from retrieval entirely; this prior settles the remainder, the
# same-subject opinions nobody has explicitly reconciled yet.
W_RECENCY_OPINION = 0.30
OPINION_RECENCY_HALF_LIFE_DAYS = 30.0

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


# Matching has to agree with the index that produced these weights. The
# document frequency behind each weight is measured by FTS5, which splits on
# non-alphanumerics, so the term "Airtable?" is counted as `airtable` in the
# denominator. A raw `in` test then never credits it, and ending a question
# with a question mark halved the lexical evidence for a perfect match.
# The same test also matched inside longer words, so "key" scored full
# conclusive-literal-evidence against "my keyboard is loud".
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokens(s: str) -> list[str]:
    """Alphanumeric runs, lowercased: how FTS5's unicode61 tokenizer splits."""
    return _TOKEN_RE.findall(s.lower())


def information_coverage(term_info: dict[str, float], text: str) -> float:
    """Share of the query's self-information this text accounts for."""
    total = sum(term_info.values())
    if total <= 0:
        return 0.0
    present = set(_tokens(text))
    got = 0.0
    for t, w in term_info.items():
        toks = _tokens(t)
        if toks and all(tok in present for tok in toks):
            got += w
    return min(_CERTAINTY_CAP, max(0.0, got / total))


def _vec_term(p_vec: float) -> float:
    """The semantic channel's contribution, on its own so recency can shift
    it without touching what the literal channel established."""
    return -W_VEC * math.log(1.0 - min(_CERTAINTY_CAP, max(0.0, p_vec)))


def _lex_term(p_lex: float) -> float:
    return -W_LEX * math.log(1.0 - min(_CERTAINTY_CAP, max(0.0, p_lex)))


def _rrf_residue(vec_rank: int | None, lex_rank: int | None) -> float:
    if not W_RRF:
        return 0.0
    residue = 0.0
    if vec_rank is not None:
        residue += 1.0 / (RRF_RESIDUE_K + vec_rank + 1)
    if lex_rank is not None:
        residue += 1.0 / (RRF_RESIDUE_K + lex_rank + 1)
    return W_RRF * residue * RRF_RESIDUE_K


def evidence(p_vec: float, p_lex: float,
             vec_rank: int | None = None, lex_rank: int | None = None) -> float:
    """Combine two independent channels as a soft OR, in log space."""
    return (_vec_term(p_vec) + _lex_term(p_lex)
            + _rrf_residue(vec_rank, lex_rank))


def recency_odds(newer_share: float | None,
                 half_life_share: float = RECENCY_HALF_LIFE_SHARE) -> float:
    """Prior odds for a memory with this share of the vault newer than it.

    1.0 for the newest memory and for anything outside the population, which
    is how a pack record and a brand new one both come through unaged. A
    half_life_share of 0 turns the whole prior off."""
    if newer_share is None or half_life_share <= 0.0:
        return 1.0
    q = min(1.0, max(0.0, float(newer_share)))
    return 2.0 ** (-q / float(half_life_share))


def aged_term(term: float, newer_share: float | None,
              half_life_share: float = RECENCY_HALF_LIFE_SHARE) -> float:
    """One evidence term, shifted by the recency prior IN ODDS.

    A term is -log P(not relevant), so its odds are e^term - 1; the prior
    multiplies those odds and this reads the result back as a term. Monotone
    in `term`, so equally aged memories keep their order, and never above
    `term`, so ageing can only cost a memory rank."""
    rho = recency_odds(newer_share, half_life_share)
    if rho >= 1.0 or term <= 0.0:
        return term
    return math.log1p(rho * math.expm1(term))


def aged_evidence(p_vec: float, p_lex: float, newer_share: float | None = None,
                  vec_rank: int | None = None, lex_rank: int | None = None,
                  half_life_share: float = RECENCY_HALF_LIFE_SHARE) -> float:
    """Evidence with the semantic channel aged and the literal one left alone.

    What a question MEANS is a guess that the vault having moved on can
    weaken. What a question NAMES it cannot: an identifier that occurs in one
    memory names that memory whatever year it was written."""
    return (aged_term(_vec_term(p_vec), newer_share, half_life_share)
            + _lex_term(p_lex) + _rrf_residue(vec_rank, lex_rank))


def prior(importance: float, created: float, now: float | None = None,
          kind: str = "fact", affirmed: float | None = None) -> float:
    """Multiplicative modulation: reranks a match, never manufactures one.

    For a fact this is importance alone, so a fact stored with the default
    0.5 carries no prior at all: its age is counted in memories rather than
    days and reaches the score through aged_evidence instead. `created`,
    `now`, `kind` and `affirmed` stay in the signature for the opinion
    branch, whose recency runs from the last re-affirmation on the shorter,
    heavier opinion constants above."""
    centred = 2.0 * float(importance) - 1.0        # 0.5 default -> exactly 0
    if kind == "opinion":
        now = time.time() if now is None else now
        ref = float(affirmed or created)
        age_days = max(0.0, (now - ref) / SECONDS_PER_DAY)
        recency = math.exp(
            -math.log(2.0) * age_days / OPINION_RECENCY_HALF_LIFE_DAYS)
        return W_IMPORTANCE * centred + W_RECENCY_OPINION * recency
    return W_IMPORTANCE * centred


def final_score(p_vec: float, p_lex: float, importance: float, created: float,
                vec_rank: int | None = None, lex_rank: int | None = None,
                now: float | None = None, kind: str = "fact",
                affirmed: float | None = None,
                newer_share: float | None = None,
                half_life_share: float = RECENCY_HALF_LIFE_SHARE) -> float:
    return aged_evidence(p_vec, p_lex, newer_share, vec_rank, lex_rank,
                         half_life_share) * (
        1.0 + prior(importance, created, now, kind=kind, affirmed=affirmed))
