"""A long memory must be searchable all the way through, not just its opening.

The encoder reads 512 tokens. Anything past that was not weighted less, it was
not seen: on a real vault of long memories that was most of the text in it.
These cover the windows that fix it, and the ways windows could go wrong.
"""
import numpy as np
import pytest

from compartment.embed import CHUNK_STRIDE, CHUNK_WINDOW, MAX_CHUNKS, Embedder
from compartment.vault import strip_provenance  # noqa: E402


@pytest.fixture(scope="module")
def emb():
    return Embedder()


# ------------------------------------------------------------------ chunking --
def test_short_text_is_one_window_and_is_left_exactly_alone(emb):
    t = "A short memory about a passphrase."
    assert emb.chunk(t) == [t]


def test_long_text_is_split_into_several_windows(emb):
    t = "sentence about vaults and agents. " * 400
    parts = emb.chunk(t)
    assert len(parts) > 1


def test_no_token_of_the_original_is_lost(emb):
    """The failure this whole mechanism exists to prevent."""
    t = " ".join(f"unique-token-{i}" for i in range(2000))
    joined = "".join(emb.chunk(t))
    for probe in ("unique-token-0", "unique-token-999", "unique-token-1999"):
        assert probe in joined


def test_consecutive_windows_overlap_so_a_fact_is_never_split(emb):
    t = " ".join(f"w{i}" for i in range(3000))
    parts = emb.chunk(t)
    assert len(parts) >= 2
    tail = parts[0][-200:]
    assert any(tail[-40:] in p for p in parts[1:2]), \
        "the second window must re-cover the end of the first"
    assert CHUNK_STRIDE < CHUNK_WINDOW


def test_window_count_is_capped_so_one_record_cannot_flood_the_index(emb):
    t = " ".join(f"w{i}" for i in range(200_000))
    assert len(emb.chunk(t)) <= MAX_CHUNKS


def test_empty_text_does_not_crash_the_splitter(emb):
    assert emb.chunk("") == [""]


def test_embed_record_returns_one_row_per_window(emb):
    short = emb.embed_record("tiny")
    assert short.shape[0] == 1
    long_text = "vaults agents memories passphrases. " * 400
    many = emb.embed_record(long_text)
    assert many.shape[0] == len(emb.chunk(long_text)) > 1
    norms = np.linalg.norm(many, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3), "windows must stay normalized"


# ------------------------------------------------------------------ recall ----
NEEDLE = ("The pairing codes are matched uppercase and word-bounded because a "
          "case-insensitive match hits Summer and Hammer.")


def _long_memory_with_needle_at_the_end() -> str:
    filler = ("This paragraph is about vault housekeeping, index rebuilds, "
              "audit chains and journal replay, and it goes on for a while. ")
    return filler * 90 + NEEDLE


def test_a_fact_past_the_encoder_window_is_retrievable(vault, emb):
    text = _long_memory_with_needle_at_the_end()
    assert len(emb.chunk(text)) > 1, "fixture must actually exceed one window"
    vault.store(text, caller="test", namespace="main")
    vault.store("An unrelated memory about printers and network ports.",
                caller="test", namespace="main")
    hits = vault.search("uppercase word boundary Summer Hammer pairing codes",
                        caller="test", top_k=3)["results"]
    assert hits and strip_provenance(hits[0]["text"]) == text


def test_the_same_fact_is_missed_when_only_the_opening_is_embedded(vault, emb):
    """Pins the failure mode, so a regression to single-vector records is loud."""
    text = _long_memory_with_needle_at_the_end()
    head_only = emb.embed_passages([text])[0]        # what the old code stored
    vault.store(text, caller="test", namespace="main", vec=head_only)
    q = emb.embed_query("uppercase word boundary Summer Hammer pairing codes")
    windows = emb.embed_record(text)
    best_window = float(np.max(windows @ q))
    assert best_window > float(head_only @ q) + 0.05, (
        "the window holding the needle must be clearly closer to the query "
        "than the record's opening is")


def test_every_window_is_removed_when_the_record_is_forgotten(vault):
    text = _long_memory_with_needle_at_the_end()
    r = vault.store(text, caller="test", namespace="main")
    assert len(vault.db.vector_keys(r["id"])) > 1
    vault.forget(r["id"], caller="test")
    assert vault.db.vector_keys(r["id"]) == []
    assert not vault.search("Summer Hammer pairing codes uppercase",
                            caller="test", top_k=5)["results"]


def test_index_keys_are_never_reused_between_records(vault):
    a = vault.store("first memory " * 200, caller="test", namespace="main")
    b = vault.store("second memory " * 200, caller="test", namespace="main")
    ka = set(vault.db.vector_keys(a["id"]))
    kb = set(vault.db.vector_keys(b["id"]))
    assert ka and kb and not (ka & kb)


def test_a_vault_written_before_windows_still_searches(vault):
    """all_vectors falls back to records.vec when the window table is empty."""
    vault.store("A memory about audit chains and journal replay.",
                caller="test", namespace="main")
    vault.db.conn.execute("DELETE FROM vecs")
    vault._rebuild_index()
    hits = vault.search("audit chain journal", caller="test", top_k=3)["results"]
    assert hits


def test_rebuild_windows_backfills_an_older_vault(vault, emb):
    text = _long_memory_with_needle_at_the_end()
    r = vault.store(text, caller="test", namespace="main")
    vault.db.conn.execute("DELETE FROM vecs")          # simulate an old vault
    vault._rebuild_index()
    report = vault.rebuild_windows(caller="test")
    assert report["rebuilt"] == 1
    assert report["windows_added"] >= 1
    assert len(vault.db.vector_keys(r["id"])) > 1
    hits = vault.search("uppercase word boundary Summer Hammer pairing codes",
                        caller="test", top_k=3)["results"]
    assert hits and strip_provenance(hits[0]["text"]) == text


def test_rebuild_windows_is_idempotent(vault):
    vault.store(_long_memory_with_needle_at_the_end(), caller="test",
                namespace="main")
    vault.rebuild_windows(caller="test")
    again = vault.rebuild_windows(caller="test")
    assert again["rebuilt"] == 0


def test_rebuild_windows_skips_short_records_without_tokenizing_them(vault):
    for i in range(5):
        vault.store(f"A short memory number {i}.", caller="test",
                    namespace="main")
    assert vault.rebuild_windows(caller="test")["examined"] == 0


# --------------------------------------------- upgrading an existing vault ---
def test_a_partly_upgraded_vault_indexes_every_record(vault):
    """The regression that shipped in 4 and had to be fixed in 4.1.

    A vault upgraded in place is PARTIAL: records written since the upgrade
    carry window rows, everything written before does not. Choosing one table
    over the other dropped every older memory out of the index while it sat
    safely in the file - 6,728 of 6,839 on a real vault.
    """
    old = [vault.store(f"An older memory about topic {i}.", caller="test",
                       namespace="main")["id"] for i in range(5)]
    vault.db.conn.execute("DELETE FROM vecs")          # they predate windows
    new = [vault.store(f"A newer memory about subject {i}.", caller="test",
                       namespace="main")["id"] for i in range(3)]
    assert vault.db.conn.execute(
        "SELECT count(DISTINCT id) c FROM vecs").fetchone()["c"] == 3

    ids, ikeys, mat = vault.db.all_vectors()
    assert set(old) | set(new) <= set(ids), "every record must reach the index"
    assert len(ikeys) == len(set(ikeys)), "index keys must stay unique"
    assert mat.shape[0] == len(ikeys)

    vault._rebuild_index()
    assert len(vault.index) == len(ikeys)
    hits = vault.search("older memory about topic", caller="test", top_k=5)
    assert any(h["id"] in old for h in hits["results"]), \
        "a record written before the upgrade must still be findable"


def test_a_record_with_windows_is_not_also_indexed_by_its_head_vector(vault):
    r = vault.store("long memory " * 400, caller="test", namespace="main")["id"]
    ids, ikeys, _ = vault.db.all_vectors()
    windows = vault.db.vector_keys(r)
    assert len(windows) > 1
    assert ids.count(r) == len(windows), \
        "a windowed record contributes its windows and nothing else"
