"""Memories that already know when they stop being true.

Most facts are permanent and stay that way: nothing here touches a memory
that was not given a last day when it was stored. What this covers is the
handful that arrive with an end on them - a shop price good for a fortnight,
a door code that changes on Monday, a rota, a booking - and the rules that
decide when one goes and when it merely says so.

The rules under test are the ones written out above `Vault.expire`. Every one
of them exists to stop a memory disappearing for a reason its owner did not
choose, so each gets a test that fails loudly if it stops holding.
"""
from __future__ import annotations

import datetime
import json

import pytest

from compartment.crypto import CryptoError
from compartment.vault import (Vault, expiry_date, strip_provenance,
                               with_provenance)

PASS = "CorrectHorse"
TODAY = datetime.date(2026, 8, 20)


def _day(offset: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


@pytest.fixture()
def v(vault_path):
    vault = Vault.create(vault_path, PASS, creator="t")
    yield vault


def _store(vault, text, **kw):
    kw.setdefault("source", "from chat")
    return vault.store(text, caller="t", **kw)


# ------------------------------------------------- saying it in few characters

@pytest.mark.parametrize("said,expected", [
    ("2w", "2026-09-03"),          # the example this was built for
    ("14d", "2026-09-03"),
    ("14", "2026-09-03"),          # a bare number is days
    ("1d", "2026-08-21"),
    ("3m", "2026-11-20"),          # calendar months, not 90-day blocks
    ("1y", "2027-08-20"),
    ("2026-09-03", "2026-09-03"),  # or just say the day
    ("2W", "2026-09-03"),          # case is not a trap
    (" 2w ", "2026-09-03"),
])
def test_the_short_forms_all_mean_the_same_day(said, expected):
    assert expiry_date(said, today=TODAY) == expected


@pytest.mark.parametrize("start,said,expected", [
    ((2026, 1, 31), "1m", "2026-02-28"),   # clamps, never rolls into March
    ((2028, 1, 31), "1m", "2028-02-29"),   # leap year
    ((2026, 2, 29 - 1), "1y", "2027-02-28"),
    ((2026, 12, 15), "2m", "2027-02-15"),  # over the year boundary
])
def test_months_and_years_are_calendar_ones(start, said, expected):
    """Somebody who says a lease runs 3m from the 20th means the 20th."""
    assert expiry_date(said, today=datetime.date(*start)) == expected


def test_no_expiry_means_permanent():
    """The overwhelmingly common case, and the one that must stay free."""
    assert expiry_date(None) is None
    assert expiry_date("") is None


def test_the_last_day_counts():
    """"For the next two weeks" includes the fourteenth day, so a fortnight
    from the 20th is the 3rd and not the 2nd."""
    assert expiry_date("2w", today=TODAY) == "2026-09-03"
    assert expiry_date("1d", today=TODAY) == "2026-08-21"


@pytest.mark.parametrize("junk", ["soon", "next tuesday", "a fortnight", "-5",
                                  "0d", "0", "2026-13-40", "2w2d", "tomorrow"])
def test_an_expiry_it_cannot_read_is_refused(junk):
    """Rule 3, half of it. This is the one date field where being wrong
    deletes something, so it never guesses."""
    with pytest.raises(CryptoError):
        expiry_date(junk, today=TODAY)


def test_a_day_already_gone_is_refused():
    with pytest.raises(CryptoError) as exc:
        expiry_date("2026-08-19", today=TODAY)
    assert "already passed" in str(exc.value)


def test_a_restore_may_repeat_a_day_that_has_gone():
    """An import repeats a date somebody else already chose. Refusing it
    there would turn one stale line into a failed restore of the file."""
    assert expiry_date("2026-08-19", today=TODAY, strict=False) == "2026-08-19"
    with pytest.raises(CryptoError):        # garbage is still garbage
        expiry_date("soon", today=TODAY, strict=False)


# --------------------------------------------------------- the memory itself

def test_a_memory_carries_its_own_shelf_life(v):
    """The clause goes into the text, so the claim stays self-describing
    wherever it ends up: exported, pasted, read by something that never saw
    this schema."""
    out = _store(v, "Bananas are 3.50 per 5 at the shop nearest me.",
                 expires="2w")
    text = v.get(out["id"], caller="t")["text"]
    assert text.endswith(f"[from chat, {_day(0)}, until {_day(14)}]")


def test_the_shelf_life_stays_out_of_the_embedding(v):
    """The clause is bookkeeping. Letting it into the vector would make every
    memory that expires on the same day look alike to the encoder."""
    text = with_provenance("A claim.", "from chat", "2026-08-20", "2026-09-03")
    assert strip_provenance(text) == "A claim."


def test_the_clause_does_not_stack_on_a_round_trip():
    once = with_provenance("A claim.", "from chat", "2026-08-20", "2026-09-03")
    twice = with_provenance(once, "from chat", "2026-08-20", "2026-09-03")
    assert once == twice
    assert twice.count("until") == 1


def test_the_date_comes_back_on_every_read_path(v):
    out = _store(v, "The lobby code is 4417.", expires="30d")
    assert out["expires"] == _day(30)
    assert v.get(out["id"], caller="t")["expires"] == _day(30)
    assert v.recent(caller="t")["results"][-1]["expires"] == _day(30)
    hit = [r for r in v.search("lobby code", caller="t")["results"]
           if r["id"] == out["id"]]
    assert hit and hit[0]["expires"] == _day(30)


def test_a_memory_with_no_expiry_reports_none_and_is_never_swept(v):
    """Rule 1. Silence means permanent, which is what almost every memory is
    and what every memory written before this existed is."""
    out = _store(v, "Max's home airport is JFK.")
    assert out.get("expires") is None
    assert v.get(out["id"], caller="t")["expires"] is None
    v.expire(caller="t")
    assert v.get(out["id"], caller="t")["id"] == out["id"]


def test_storing_something_already_dead_is_refused(v):
    with pytest.raises(CryptoError):
        _store(v, "The sale ended.", expires=_day(-1))


# ------------------------------------------------------------------ the sweep

def test_the_sweep_takes_the_expired_and_leaves_everything_else(v):
    dead = _store(v, "The lobby code is 4417.", expires=_day(-1),
                  _expiry_strict=False)
    live = _store(v, "Bananas are 3.50 per 5 nearby.", expires="2w")
    perm = _store(v, "Max's home airport is JFK.")
    out = v.expire(caller="t")
    assert out["enabled"] is True and out["removed"] == 1
    assert out["ids"] == [dead["id"]]
    assert v.get(live["id"], caller="t")["id"] == live["id"]
    assert v.get(perm["id"], caller="t")["id"] == perm["id"]
    with pytest.raises(CryptoError):
        v.get(dead["id"], caller="t")


def test_the_day_itself_is_still_live(v):
    """Rule 2, where it counts: a memory that expires today survives today."""
    out = _store(v, "Today only: the code is 4417.", expires=_day(0))
    assert v.expire(caller="t")["removed"] == 0
    assert v.get(out["id"], caller="t")["expires"] == _day(0)


def test_a_swept_memory_leaves_no_window_in_the_index(v):
    """Rule 7. A window left behind keeps answering searches for text the
    vault no longer holds, which is the one thing a removal must never do."""
    _store(v, "The lobby door code is 4417 until further notice.",
           expires=_day(-1), _expiry_strict=False)
    v.expire(caller="t")
    hits = v.search("lobby door code", caller="t")["results"]
    assert not any("4417" in r["text"] for r in hits)


def test_the_sweep_is_written_into_the_audit_log(v):
    """Rule 7. A memory never vanishes with no record that it did."""
    dead = _store(v, "The lobby code is 4417.", expires=_day(-1),
                  _expiry_strict=False)
    v.expire(caller="t")
    rows = list(v.db.conn.execute(
        "SELECT op, detail FROM audit WHERE op = 'expire'"))
    assert rows and dead["id"] in rows[-1]["detail"]


def test_a_sweep_with_nothing_to_do_writes_nothing(v):
    """Otherwise every open of every vault would append to the audit chain
    and the journal for no reason at all."""
    _store(v, "Max's home airport is JFK.")
    before = v.db.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
    assert v.expire(caller="t")["removed"] == 0
    after = v.db.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
    assert after == before


def test_pack_records_can_never_expire(v, monkeypatch):
    """Rule 4. Pack and seed content is the shipped product, identical on
    every machine. A sweep deleting part of it would be a bug that looks
    exactly like data loss."""
    v.store("A curated starting fact.", caller="t", pack="starter",
            source="pack", expires="1d")
    row = v.db.conn.execute(
        "SELECT expires FROM records WHERE pack IS NOT NULL").fetchone()
    assert row["expires"] is None
    assert v.db.expired_candidates("2099-01-01") == []


def test_a_seeded_starting_memory_can_never_be_swept(v):
    """Rule 4, the half `store` cannot enforce.

    A seeded memory goes in as an ordinary record with `pack` NULL, marked
    only by an "id:" tag, so the refusal on pack content never sees one. If
    a future pack format grew an expiry field, nothing else would stop a
    sweep deleting the memories that shipped with the vault.
    """
    out = v.store("A starting memory.", caller="t", source="pack",
                  tags=["id:starter-1"], expires=_day(-1),
                  _expiry_strict=False)
    # It is on the record, and the sweep still refuses to touch it.
    assert v.get(out["id"], caller="t")["expires"] == _day(-1)
    assert v._expired_today() == []
    assert v.expire(caller="t")["removed"] == 0
    assert v.get(out["id"], caller="t")["id"] == out["id"]
    assert v.expiring(caller="t") == []


# ------------------------------------------------------------------ the toggle

def test_with_the_toggle_off_nothing_is_ever_deleted(v):
    """Rule 5. The date becomes a label: recorded, shown, and acted on by
    nobody."""
    v.config.settings["expire_memories"] = False
    dead = _store(v, "The old wifi password is hunter2.", expires=_day(-1),
                  _expiry_strict=False)
    out = v.expire(caller="t")
    assert out["enabled"] is False and out["removed"] == 0
    assert v.get(dead["id"], caller="t")["expires"] == _day(-1)


def test_with_the_toggle_off_an_expired_memory_stays_searchable(v):
    v.config.settings["expire_memories"] = False
    dead = _store(v, "The old wifi password is hunter2.", expires=_day(-1),
                  _expiry_strict=False)
    hits = v.search("wifi password", caller="t")["results"]
    assert any(r["id"] == dead["id"] for r in hits)


def test_with_the_toggle_on_an_expired_memory_is_never_handed_over(v):
    """Rule 9. Being told a price that expired is worse than not being told
    it, so the read paths filter even inside the hour before the sweep."""
    dead = _store(v, "The old wifi password is hunter2.", expires=_day(-1),
                  _expiry_strict=False)
    v._last_expiry_sweep = 9e18          # pretend a sweep just ran
    hits = v.search("wifi password", caller="t")["results"]
    assert not any(r["id"] == dead["id"] for r in hits)
    assert not any(r["id"] == dead["id"]
                   for r in v.recent(caller="t")["results"])


def test_turning_the_toggle_off_does_not_bring_anything_back(v):
    """Rule 5, the second half. Off stops the next sweep; it is not an undo."""
    dead = _store(v, "The lobby code is 4417.", expires=_day(-1),
                  _expiry_strict=False)
    v.expire(caller="t")
    v.config.settings["expire_memories"] = False
    with pytest.raises(CryptoError):
        v.get(dead["id"], caller="t")


def test_the_toggle_is_on_by_default():
    """An expiry the user took the trouble to set should do something."""
    from compartment.acl import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["settings"]["expire_memories"] is True


# -------------------------------------------------------------- when it runs

def test_a_read_sweeps_at_most_once_an_hour(v, monkeypatch):
    """Rule 6. Opening alone is not enough - the MCP server holds one vault
    open for weeks - and an hour is fine grain for a date."""
    calls = []
    monkeypatch.setattr(Vault, "expire",
                        lambda self, caller="expiry": calls.append(caller))
    v._last_expiry_sweep = 0.0
    v.search("anything", caller="t")
    assert len(calls) == 1
    v._last_expiry_sweep = __import__("time").time()
    v.search("anything again", caller="t")
    assert len(calls) == 1, "swept twice inside the hour"


def test_a_sweep_that_fails_does_not_take_the_search_down(v, monkeypatch):
    def boom(self, caller="expiry"):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(Vault, "expire", boom)
    v._last_expiry_sweep = 0.0
    assert "results" in v.search("anything", caller="t")


def test_opening_a_vault_sweeps_it(vault_path):
    """Rule 6, the first half, through a real unlock."""
    v = Vault.create(vault_path, PASS, creator="t")
    dead = _store(v, "The lobby code is 4417.", expires=_day(-1),
                  _expiry_strict=False)
    v.save()
    v.lock()
    again = Vault.unlock(vault_path, passphrase=PASS)
    with pytest.raises(CryptoError):
        again.get(dead["id"], caller="t")


# ------------------------------------------------------- carrying it around

def test_export_and_import_carry_the_date(v, vault_path, tmp_path):
    _store(v, "Bananas are 3.50 per 5 nearby.", expires="2w")
    line = json.loads(v.export_jsonl().strip())
    assert line["expires"] == _day(14)

    other = Vault.create(str(tmp_path / "other.vault"), PASS, creator="t")
    other.import_jsonl(v.export_jsonl(), caller="t")
    got = other.recent(caller="t")["results"][-1]
    assert got["expires"] == _day(14)


def test_a_restore_does_not_resurrect_something_already_dead(v, tmp_path):
    """It would live until the next sweep and then vanish again, which reads
    as an import that quietly lost records."""
    _store(v, "The old wifi password is hunter2.", expires=_day(-1),
           _expiry_strict=False)
    other = Vault.create(str(tmp_path / "other.vault"), PASS, creator="t")
    other.import_jsonl(v.export_jsonl(), caller="t")
    assert other.recent(caller="t")["counts"]["organic"] == 0


def test_a_restore_keeps_it_when_expiry_is_only_a_label(v, tmp_path):
    _store(v, "The old wifi password is hunter2.", expires=_day(-1),
           _expiry_strict=False)
    other = Vault.create(str(tmp_path / "other.vault"), PASS, creator="t")
    other.config.settings["expire_memories"] = False
    other.import_jsonl(v.export_jsonl(), caller="t")
    assert other.recent(caller="t")["counts"]["organic"] == 1


# ----------------------------------------------------------- older vaults

def test_a_vault_sealed_before_this_column_existed_still_opens(vault_path):
    """New COLUMNS do not arrive for free the way new tables do: `CREATE
    TABLE IF NOT EXISTS` is a no-op the moment the table exists, so without
    the migration an older vault would deserialize and then fail on the first
    query naming `expires`."""
    import sqlite3

    from compartment.store import Store

    # A records table shaped the way one was before this column existed.
    # Built by hand rather than by dropping the column from a current one:
    # ALTER TABLE ... DROP COLUMN re-parses the stored schema text, and this
    # schema carries SQL comments that do not survive that round trip.
    old = sqlite3.connect(":memory:")
    old.executescript("""
        CREATE TABLE records (
            id TEXT PRIMARY KEY, ikey INTEGER UNIQUE, ns TEXT NOT NULL,
            ct BLOB NOT NULL, key_wrapped BLOB NOT NULL, vec BLOB NOT NULL,
            dim INTEGER NOT NULL, tags TEXT NOT NULL, importance REAL NOT NULL,
            quarantined INTEGER NOT NULL, pack TEXT, prov TEXT NOT NULL,
            created REAL NOT NULL, accessed REAL NOT NULL, source TEXT,
            discovered TEXT, tags_origin TEXT);
        INSERT INTO records VALUES ('old1', 1, 'main', x'00', x'00', x'00', 4,
            '[]', 0.5, 0, NULL, '{}', 0, 0, NULL, NULL, '[]');
    """)
    image = old.serialize()

    aged = Store(image)                          # opening runs the migration
    have = {r["name"] for r in
            aged.conn.execute("PRAGMA table_info(records)")}
    assert "expires" in have
    # And the queries that name it work, rather than raising on first use.
    assert aged.expired_candidates("2099-01-01") == []
    assert aged.expiring_count() == 0
    assert aged.conn.execute(
        "SELECT expires FROM records WHERE id = 'old1'").fetchone()[0] is None


def test_status_reports_what_carries_a_date(v):
    _store(v, "Bananas are 3.50 per 5 nearby.", expires="2w")
    _store(v, "Max's home airport is JFK.")
    st = v.status()
    assert st["expiring_records"] == 1
    assert st["expire_memories"] is True
    assert st["expired_pending"] == 0


def test_status_counts_what_is_waiting_while_the_toggle_is_off(v):
    v.config.settings["expire_memories"] = False
    _store(v, "The old wifi password is hunter2.", expires=_day(-1),
           _expiry_strict=False)
    assert v.status()["expired_pending"] == 1


# ------------------------------------------------------- through the tools

def test_the_mcp_tool_takes_an_expiry(vault_path, monkeypatch):
    """The path a model actually uses. `expires` is per-fact on the batch
    tool and has no call-level default: a wrong source mislabels a memory,
    a wrong expiry deletes every memory in the batch."""
    import inspect

    from compartment import server
    assert "expires" in inspect.signature(server.memory_store).parameters
    assert "expires" not in inspect.signature(
        server.memory_store_many).parameters


def test_the_batch_tool_passes_each_facts_own_expiry(v):
    """Straight through the real vault, because what matters is that fact two
    does not inherit fact one's last day."""
    one = _store(v, "Bananas are 3.50 per 5 nearby.", expires="2w")
    two = _store(v, "Max's home airport is JFK.")
    assert v.get(one["id"], caller="t")["expires"] == _day(14)
    assert v.get(two["id"], caller="t")["expires"] is None


def test_the_cli_stores_and_clears(vault_path, capsys):
    """`compartment expire` exists because the automatic sweep covers opening
    and the top of the hour, and not the two moments a person wants: seeing
    what is about to go, and clearing it now."""
    from argparse import Namespace

    from compartment import cli
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    _store(v, "The lobby code is 4417.", expires=_day(-1),
           _expiry_strict=False)
    _store(v, "Bananas are 3.50 per 5 nearby.", expires="2w")
    session.store(vault_path, v._master)   # what `compartment unlock` leaves
    v.save()
    v.lock()
    args = Namespace(vault=vault_path, caller="t", list=True, enable=False,
                     disable=False, passphrase=PASS, keyfile=None)
    cli.cmd_expire(args)
    out = capsys.readouterr().out
    # Opening the vault swept the dead one, which is rule 6 doing its job.
    # What --list is for is the live one and the day it goes.
    assert "1 memory with an expiry" in out
    assert "bananas" in out.lower() and _day(14) in out


def test_the_cli_can_reach_the_toggle_without_the_app(vault_path, capsys):
    """A headless box has no panel, and a setting that is only reachable from
    one is out of reach on exactly the machines run from a terminal.

    No credential is stored here on purpose. The setting lives in
    <vault>.config.json, which holds no secrets, and the panel changes it on
    a locked vault for that reason: needing a passphrase to flip a preference
    would be a lock on the wrong door. If that regresses, this test stops on
    a getpass prompt rather than passing quietly.
    """
    from argparse import Namespace

    from compartment import cli
    from compartment.acl import VaultConfig
    Vault.create(vault_path, PASS, creator="t").lock()
    base = dict(vault=vault_path, caller="t", list=False, passphrase=None,
                keyfile=None)
    cli.cmd_expire(Namespace(**base, enable=False, disable=True))
    assert VaultConfig.load(vault_path).settings["expire_memories"] is False
    cli.cmd_expire(Namespace(**base, enable=True, disable=False))
    assert VaultConfig.load(vault_path).settings["expire_memories"] is True


def test_a_journalled_sweep_replays(vault_path):
    """`_replay` raises TamperError on an op it does not know, so a vault
    whose journal happened to record a sweep would refuse to open at all."""
    v = Vault.create(vault_path, PASS, creator="t")
    dead = _store(v, "The lobby code is 4417.", expires=_day(-1),
                  _expiry_strict=False)
    keep = _store(v, "Max's home airport is JFK.")
    v.expire(caller="t")                      # journalled, not yet compacted
    again = Vault.unlock(vault_path, passphrase=PASS)
    assert again.get(keep["id"], caller="t")["id"] == keep["id"]
    with pytest.raises(CryptoError):
        again.get(dead["id"], caller="t")


def test_a_long_sweep_says_its_audit_line_is_capped(v):
    """An audit line is read by a person, so the id list is capped. A cap
    that trails off silently reads as a complete list."""
    for i in range(25):
        _store(v, f"Transient fact number {i}, about item {i}.",
               expires=_day(-1), _expiry_strict=False)
    out = v.expire(caller="t")
    assert out["removed"] == 25 and len(out["ids"]) == 25
    detail = list(v.db.conn.execute(
        "SELECT detail FROM audit WHERE op = 'expire'"))[-1]["detail"]
    assert "n=25" in detail and "+5 more" in detail
    # and the journal is not capped
    assert len(out["ids"]) == 25
