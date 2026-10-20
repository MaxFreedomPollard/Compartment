"""Stored instants that arrive from outside the vault.

`created` and `affirmed` reach the vault from files on the restore paths. A
value stored as it came - an ISO string, a NaN, an infinity, milliseconds -
used to break every later search, because every search reads every live
record's time for the recency prior.
"""
import datetime
import json
import time

import pytest

from compartment.vault import Vault, instant, local_stamp

from conftest import PASS


def _line(**fields) -> str:
    return json.dumps(fields) + "\n"


def test_instant_reads_what_a_person_would_mean():
    t = datetime.datetime(2024, 5, 1, 9, 30,
                          tzinfo=datetime.timezone.utc).timestamp()
    assert instant(t) == t
    assert instant(str(t)) == t
    assert instant("2024-05-01T09:30:00Z") == t
    assert instant("2024-05-01T09:30:00+00:00") == t
    assert instant(t * 1000) == t, "JavaScript's Date.now() is milliseconds"
    local_midnight = datetime.datetime(2024, 5, 1).timestamp()
    assert instant("2024-05-01") == local_midnight
    for nonsense in (None, "", "yesterday", True, float("nan"),
                     float("inf"), -float("inf"), 1e300, [1]):
        assert instant(nonsense) is None, nonsense


def test_an_infinite_date_is_shown_as_no_date_instead_of_raising():
    assert local_stamp(float("inf")) == ""
    assert local_stamp(1e300) == ""


@pytest.mark.parametrize("created", [
    "2024-05-01T09:30:00Z", float("inf"), float("nan"), "last tuesday",
    1714555800000])
def test_an_imported_date_never_breaks_search(vault, created):
    """An ISO string went into the REAL column as text and the next search
    failed converting it; infinity was stored and failed every reader of
    the record; NaN failed the import half way."""
    vault.store("Tea is kept in the left cupboard.", caller="t")
    before = time.time()
    n = vault.import_jsonl(
        json.dumps({"text": "Coffee beans are kept in the freezer.",
                    "created": created}, allow_nan=True) + "\n",
        caller="t")
    assert n == 1
    hits = vault.search("where are the coffee beans kept", caller="t")
    top = hits["results"][0]
    assert top["text"].startswith("Coffee beans are kept in the freezer.")
    assert isinstance(top["created"], float)
    assert top["created_local"], "the date reads as a date"
    want = instant(created)
    if want is None:
        assert top["created"] >= before, "an unreadable date becomes now"
    else:
        assert top["created"] == want
    assert vault.recent("t")["results"]


def test_an_unreadable_affirmed_time_is_dropped(vault):
    vault.import_jsonl(_line(text="Prefers tabs over spaces.",
                             kind="opinion", affirmed="recently"),
                       caller="t")
    row = vault.db.conn.execute("SELECT affirmed FROM records").fetchone()
    assert row["affirmed"] is None
    assert vault.search("tabs or spaces", caller="t")["results"]


def test_a_vault_an_older_build_broke_is_repaired_when_it_opens(vault,
                                                                vault_path):
    """A vault that took such a line before this build failed every search
    for good, and the record could not be found to remove it."""
    good = vault.store("Tea is kept in the left cupboard.", caller="t")["id"]
    bad = vault.store("Coffee beans are kept in the freezer.",
                      caller="t")["id"]
    vault.db.conn.execute(
        "UPDATE records SET created = ?, affirmed = ? WHERE id = ?",
        ("2024-05-01T09:30:00Z", "soon", bad))
    vault.db._invalidate_recency()
    with pytest.raises(ValueError):           # what an older build did
        vault.search("coffee", caller="t")
    vault.save()

    again = Vault.unlock(vault_path, passphrase=PASS)
    hits = again.search("where are the coffee beans kept", caller="t")
    assert hits["results"][0]["id"] == bad
    assert hits["results"][0]["created"] == instant("2024-05-01T09:30:00Z")
    row = again.db.get_row(bad)
    assert row["affirmed"] is None
    assert again.db.get_row(good)["created"] == vault.db.get_row(good)[
        "created"], "a record with a good date is untouched"
    ops = [r["op"] for r in again.db.conn.execute("SELECT op FROM audit")]
    assert "repair" in ops
    # and the repair was compacted in: the next open has nothing to do
    third = Vault.unlock(vault_path, passphrase=PASS)
    assert [r["op"] for r in third.db.conn.execute(
        "SELECT op FROM audit")].count("repair") == 1
