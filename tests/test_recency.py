"""Recency counted in memories: the population, its cache, and recall.

A memory's age here is the share of the vault's own memories written after
it, so the vault has to know which records are its own and how many came
later. The pure functions that turn that share into a score live in
tests/test_ranking.py; these are about the store that counts and the search
that reads the count.
"""
import time

import numpy as np
import pytest

from compartment import ranking as R
from compartment.vault import Vault, is_seeded

PASS = "CorrectHorse"
DAY = 86400.0


def _store(v, text, **kw):
    """A store with the authoring guards off: these tests write backdated and
    sometimes repeated text, which is not what those guards are for."""
    return v.store(text, caller="test", _gate=False, _dedup=False, **kw)


# --------------------------------------------------------- the population ---

def test_the_population_is_one_reference_time_per_live_organic_record(vault):
    now = time.time()
    _store(vault, "The kettle lives in the left cupboard", created=now - 3 * DAY)
    _store(vault, "The spare key is with the neighbour", created=now - DAY)
    assert vault.db.recency_times().tolist() == [
        pytest.approx(now - 3 * DAY), pytest.approx(now - DAY)]


def test_the_population_comes_back_oldest_first(vault):
    now = time.time()
    for offset in (5.0, 1.0, 9.0, 3.0):
        _store(vault, f"Memory {offset} about the garage shelves",
               created=now - offset * DAY)
    times = vault.db.recency_times()
    assert list(times) == sorted(times)


def test_a_reaffirmed_memory_counts_from_its_affirmation(vault):
    """An opinion restated today IS a recent memory, whatever year it was
    first held."""
    now = time.time()
    old = _store(vault, "Max prefers the aisle seat on long flights",
                 kind="opinion", created=now - 400 * DAY)
    vault.db.reaffirm(old["id"], now)
    assert vault.db.recency_times().tolist() == [pytest.approx(now)]


def test_a_superseded_memory_is_not_in_the_population(vault):
    now = time.time()
    a = _store(vault, "The API tier in use is the free one", created=now - DAY)
    _store(vault, "The API tier in use is the paid one", created=now,
           supersedes=[a["id"]])
    assert vault.db.recency_times().tolist() == [pytest.approx(now)]


def test_a_pack_record_is_not_in_the_population(vault):
    now = time.time()
    _store(vault, "The office printer is on the second floor", created=now)
    vault.store("A curated fact that arrived in a pack", caller="test",
                namespace="packs/demo", pack="demo", created=now - DAY,
                _gate=False, _dedup=False)
    assert vault.db.recency_times().tolist() == [pytest.approx(now)]


def test_the_starting_memories_are_not_in_the_population(seeded_vault):
    """Thousands of them arrive at one instant. Counted, they would park a
    user's first few hundred own memories at the fresh end of the vault and
    the prior would do nothing for exactly the people who just installed it.

    Written with _journal=False and never saved, and counted as a delta:
    `seeded_vault` is shared across the session, so nothing here may reach
    its file and other tests have already put a memory or two in it.
    """
    v = seeded_vault
    assert v.db.count() > 6000
    before = v.db.recency_times().size
    assert before == v.status()["organic_records"]
    assert before < 100, "the 6,665 starting memories are not the vault's own"
    for i in range(3):
        _store(v, f"A memory the agent learned during use, number {i}",
               _journal=False)
    assert v.db.recency_times().size == before + 3
    assert v.status()["organic_records"] == before + 3


def test_memories_stored_at_one_instant_age_as_one_block(vault):
    """Spec: a record's own time does not count against it, so a bulk import
    stamped at one moment is one tie block rather than a queue in which each
    record is older than the one beside it. This is the strictly-newer rule,
    and it is the whole difference between side="right" and side="left"."""
    at_once = 1_700_000_000.0
    ids = [_store(vault, f"Imported memory number {i} about the archive",
                  created=at_once)["id"] for i in range(6)]
    shares = [Vault._newer_share(vault.db.rank_row(rid),
                                 vault.db.recency_times()) for rid in ids]
    assert shares == [0.0] * 6, "nothing is newer, so nothing in the block ages"

    _store(vault, "One memory written after the import", created=at_once + 60)
    shares = [Vault._newer_share(vault.db.rank_row(rid),
                                 vault.db.recency_times()) for rid in ids]
    assert shares == [pytest.approx(1 / 7)] * 6, (
        "one record is newer than the block, so each of the six is 1 of 7")


def test_the_population_is_scoped_to_the_namespaces_asked_for(vault):
    """In a shared vault, one agent writing thousands of records in a week
    must not age another agent's whole history."""
    now = time.time()
    _store(vault, "A memory in the main namespace", created=now - DAY)
    _store(vault, "A memory in the project namespace", namespace="project",
           created=now)
    assert vault.db.recency_times({"main"}).tolist() == [
        pytest.approx(now - DAY)]
    assert vault.db.recency_times({"project"}).tolist() == [pytest.approx(now)]
    assert vault.db.recency_times({"main", "project"}).size == 2
    assert vault.db.recency_times(set()).size == 0
    assert vault.db.recency_times().size == 2


# ------------------------------------------------- keeping the count fresh ---

def test_storing_a_memory_shows_up_in_the_population_at_once(vault):
    assert vault.db.recency_times().size == 0
    _store(vault, "The bin goes out on Tuesday night")
    assert vault.db.recency_times().size == 1


def test_forgetting_a_memory_takes_it_out_of_the_population(vault):
    rid = _store(vault, "The guest wifi password is on the fridge")["id"]
    assert vault.db.recency_times().size == 1
    vault.forget(rid, caller="test")
    assert vault.db.recency_times().size == 0


def test_superseding_a_memory_takes_it_out_of_the_population(vault):
    a = _store(vault, "The standing desk is 74 cm high")["id"]
    b = _store(vault, "The standing desk is 76 cm high")["id"]
    assert vault.db.recency_times().size == 2
    vault.supersede(a, b, caller="test")
    assert vault.db.recency_times().size == 1


def test_restoring_a_superseded_memory_puts_it_back(vault):
    """Forgetting a correction means the original stands again, and the
    count has to say so."""
    a = _store(vault, "The lease runs to the end of March")["id"]
    b = _store(vault, "The lease runs to the end of June")["id"]
    vault.supersede(a, b, caller="test")
    assert vault.db.recency_times().size == 1
    vault.forget(b, caller="test")
    assert vault.db.recency_times().size == 1
    assert not vault.get(a, caller="test").get("superseded_by")


def test_reaffirming_a_memory_moves_its_time_to_the_front(vault):
    """The one mutation the document-frequency cache does not care about:
    FTS is untouched, but the reference time has moved."""
    now = time.time()
    old = _store(vault, "Max prefers dark mode in every editor",
                 kind="opinion", created=now - 400 * DAY)["id"]
    _store(vault, "The kitchen radio is a DAB one", created=now - 200 * DAY)
    before = vault.db.recency_times().tolist()
    assert before[0] == pytest.approx(now - 400 * DAY)
    vault.db.reaffirm(old, now)
    after = vault.db.recency_times().tolist()
    assert after[-1] == pytest.approx(now)
    assert after[0] == pytest.approx(now - 200 * DAY)


def test_the_legacy_starter_merge_rebuilds_the_population(vault):
    """It rewrites `ns` and `pack` with direct SQL, which is the population's
    own predicate, so it has to drop the cached count itself."""
    vault.store("A starting memory from a legacy vault layout", caller="test",
                namespace="packs/starter", pack="starter",
                _gate=False, _dedup=False)
    assert vault.db.recency_times().size == 0
    assert vault._merge_legacy_starter() == 1
    assert vault.db.recency_times().size == 1


# ------------------------------------------------------------ recall order ---

def test_the_newer_of_two_identical_memories_is_returned_first(vault):
    """Same text, so the same cosine and the same literal evidence. Age is
    the only thing left to order them by."""
    now = time.time()
    text = "The office wifi password is written on the whiteboard"
    old = _store(vault, text, created=now - 400 * DAY)
    new = _store(vault, text, created=now - DAY)
    hits = vault.search("office wifi password", caller="test")["results"]
    ids = [h["id"] for h in hits]
    assert ids[:2] == [new["id"], old["id"]]


class FixedCosines:
    """A stand-in encoder that hands the test the cosine it asks for.

    The tests below assert a RATIO between an aged score and an unaged one
    over several hundred memories, which needs a cosine that is known and a
    store that costs nothing. Every other vault test uses the real model.
    """
    dim = 384

    def __init__(self, cosines: dict[str, float]):
        self.cosines = cosines

    def _unit(self, cos: float) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        v[0] = cos
        v[1] = float(np.sqrt(max(0.0, 1.0 - cos * cos)))
        return v

    def embed_query(self, text):
        return self._unit(1.0)

    def embed_record(self, text):
        return np.atleast_2d(self._unit(self.cosines.get(text, 0.0)))

    def chunk(self, text):
        return [text]


TARGET = "The mail provider decision was settled on a Tuesday"
#: Shares no token with any memory stored below, so the keyword channel stays
#: silent and the semantic term, the one recency shifts, is the whole score.
QUERY = "pomelo lantern sequence"


def _filled_vault(tmp_path, name, older, newer, target_created):
    """A vault holding one findable memory and a crowd of unfindable ones,
    `older` of them before it and `newer` after."""
    v = Vault.create(str(tmp_path / name), PASS, creator="test")
    v._embedder = FixedCosines({TARGET: 0.65})
    _store(v, TARGET, created=target_created)
    for i in range(older):
        _store(v, f"Filler memory {i} from before", created=target_created - i - 1)
    for i in range(newer):
        _store(v, f"Filler memory {i} from after", created=target_created + i + 1)
    return v


def _target_score(v, half_life_share=None):
    """The target's published score, with the knob set or left alone."""
    if half_life_share is None:
        v.config.settings.pop("recency_half_life_share", None)
    else:
        v.config.settings["recency_half_life_share"] = half_life_share
    hits = v.search(QUERY, caller="test")["results"]
    return next(h["score"] for h in hits if h["text"].startswith(TARGET))


def test_a_two_week_old_memory_in_a_quiet_vault_keeps_its_score(tmp_path):
    """The request, first half. Two memories written since, out of three
    hundred: the vault has not moved on, so neither has the answer."""
    now = time.time()
    v = _filled_vault(tmp_path, "quiet.vault", older=300, newer=2,
                      target_created=now - 14 * DAY)
    assert _target_score(v) >= 0.99 * _target_score(v, half_life_share=0)


def test_half_the_vault_being_newer_cuts_a_memorys_score(tmp_path):
    """The request, second half. The same fortnight, but half the vault was
    written after it."""
    now = time.time()
    v = _filled_vault(tmp_path, "busy.vault", older=150, newer=150,
                      target_created=now - 14 * DAY)
    assert _target_score(v) <= 0.7 * _target_score(v, half_life_share=0)


def test_a_fresh_memory_outranks_an_equally_relevant_old_one(tmp_path):
    now = time.time()
    v = _filled_vault(tmp_path, "race.vault", older=100, newer=100,
                      target_created=now - 200 * DAY)
    fresh = "The mail provider decision was settled on a Tuesday, restated"
    v._embedder.cosines[fresh] = 0.65
    _store(v, fresh, created=now)
    hits = v.search(QUERY, caller="test")["results"]
    assert hits[0]["text"].startswith(fresh)


def test_the_only_relevant_memory_is_returned_even_when_it_is_the_oldest(
        tmp_path):
    """Membership is decided on the unaged evidence. Whether a match is real
    is not a question a memory's age can answer, and without this rule a
    vault would answer "nothing found" about the one thing it knows."""
    now = time.time()
    v = _filled_vault(tmp_path, "oldest.vault", older=0, newer=300,
                      target_created=now - 400 * DAY)
    hits = v.search(QUERY, caller="test")["results"]
    assert [h["text"].startswith(TARGET) for h in hits] == [True]
    assert hits[0]["score"] < R.RESULT_ABSOLUTE_FLOOR, (
        "the aged score is below the floor it passed, which is the whole "
        "point: the floor read the unaged one")


def test_an_explicit_top_k_is_the_first_k_of_the_recency_order(tmp_path):
    now = time.time()
    v = Vault.create(str(tmp_path / "window.vault"), PASS, creator="test")
    texts = [f"The mail provider decision, version {i}" for i in range(3)]
    v._embedder = FixedCosines({t: 0.7 for t in texts})
    ids = [_store(v, t, created=now - (3 - i) * DAY)["id"]
           for i, t in enumerate(texts)]
    hits = v.search(QUERY, caller="test", top_k=2)["results"]
    assert [h["id"] for h in hits] == [ids[2], ids[1]]


# ------------------------------------------------- the starting memories ---

def test_a_starting_memory_is_scored_without_recency(seeded_vault):
    """The vault's own memories age; the ones that arrived with it do not.

    The two organic records are stored inside the test on purpose. Without
    them the population can be empty, _newer_share short-circuits at n < 2,
    nothing in the vault ages, and the test would pass whether the seeded
    exemption were there or not. With them, an old organic record IS aged in
    the same call, which is what gives the seeded half of the assertion its
    teeth. Written with _journal=False and never saved, so nothing reaches
    the shared fixture's file.
    """
    v = seeded_vault
    now = time.time()
    old = _store(v, "The zarquon manifold note the agent wrote down",
                 created=now - 400 * DAY, _journal=False)["id"]
    _store(v, "A memory about something else the agent learned later",
           created=now, _journal=False)
    query = "zarquon manifold"
    fused, _cosine, static = v._rank_candidates(
        query, v.embedder.embed_query(query), R.CANDIDATE_POOL)
    assert fused[old] < static[old], "the vault's own old memory must age"
    seeded = [rid for rid in fused if is_seeded(v.db.get_row(rid)["tags"])]
    assert seeded, "the seeded vault should produce starting memories"
    for rid in seeded:
        assert fused[rid] == pytest.approx(static[rid])


def test_a_starting_memory_is_still_found(seeded_vault):
    hits = seeded_vault.search("what is a vector embedding",
                               caller="test")["results"]
    assert hits


# -------------------------------------------------------------- the knob ---

def test_the_half_life_share_setting_can_turn_the_prior_off(tmp_path):
    now = time.time()
    v = _filled_vault(tmp_path, "off.vault", older=50, newer=50,
                      target_created=now - 14 * DAY)
    assert _target_score(v, half_life_share=0) > _target_score(v)


def test_a_smaller_half_life_share_ages_a_memory_faster(tmp_path):
    now = time.time()
    v = _filled_vault(tmp_path, "faster.vault", older=50, newer=50,
                      target_created=now - 14 * DAY)
    assert _target_score(v, half_life_share=0.25) < _target_score(v)


def test_an_unusable_half_life_share_falls_back_to_the_default(vault):
    """A hand-edited config file accepts any key, so this one has to survive
    whatever is written in it rather than take the vault down."""
    for junk in (None, "0.25", -1.0, True, [], {}):
        vault.config.settings["recency_half_life_share"] = junk
        assert vault._recency_half_life() == R.RECENCY_HALF_LIFE_SHARE
    vault.config.settings.pop("recency_half_life_share")
    assert vault._recency_half_life() == R.RECENCY_HALF_LIFE_SHARE
    vault.config.settings["recency_half_life_share"] = 0.25
    assert vault._recency_half_life() == 0.25
    vault.config.settings["recency_half_life_share"] = 0
    assert vault._recency_half_life() == 0.0
