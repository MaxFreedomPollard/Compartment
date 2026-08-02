"""The starting memories arrive with every install, and every surface says so.

Two separate failures hide behind "where are the starter memories?". One is an
install that never seeded and said nothing about it. The other is an install
that seeded perfectly and then reported itself as empty, because the recent
feed hides seeded records by design. Both look identical to the person who
just installed it, so both are tested here.
"""
import json
import pathlib

import pytest

from compartment import cli, menubar, packs
from compartment.acl import VaultConfig
from compartment.vault import Vault, is_seeded
from compartment.vault import strip_provenance  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parents[1] / "src" / "compartment" / "data"
PASS = "CorrectHorse"


# --------------------------------------------------------------- the install

def test_a_missing_starter_pack_is_a_hard_failure(monkeypatch, tmp_path):
    """Never quietly hand back an empty vault."""
    monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path / "not-here")
    with pytest.raises(SystemExit):
        cli._seed_blobs()


def test_the_failure_names_the_remedy(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path / "not-here")
    with pytest.raises(SystemExit):
        cli._seed_blobs()
    err = capsys.readouterr().err
    assert "starter.mpack" in err and "reinstall" in err.lower()


def test_a_broken_install_leaves_no_vault_behind(monkeypatch, tmp_path):
    """The check runs before the passphrase prompt and before any write, so a
    failed init cannot leave a half-made vault that `init` then refuses to
    overwrite."""
    monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path / "not-here")
    path = tmp_path / "v.vault"
    args = _init_args(str(path))
    with pytest.raises(SystemExit):
        cli.cmd_init(args)
    assert not path.exists()


def test_the_shipped_install_finds_its_seed():
    blobs = cli._seed_blobs()
    assert [n for n, _ in blobs] == ["starter"]
    assert len(blobs[0][1]) > 1_000_000


def _init_args(path):
    class A:
        vault = None
        passphrase = PASS
        creator = "test"
        keychain = False
        no_app = True
        keyfile = None
    a = A()
    a.vault = path
    return a


# ------------------------------------------------------------- the reporting

@pytest.fixture(scope="module")
def _own_seeded(tmp_path_factory):
    """This module's own vault. The shared session fixture is written to by
    other tests, and these assertions are about exactly which records come
    back."""
    p = str(tmp_path_factory.mktemp("starter-vis") / "v.vault")
    v = Vault.create(p, PASS, creator="test")
    packs.seed_records(v, (DATA / "starter.mpack").read_bytes(), caller="test")
    return v


@pytest.fixture()
def sv(_own_seeded):
    """Settings are per-vault and this one is reused, so put the toggle back."""
    _own_seeded.config.settings["search_starter_facts"] = True
    yield _own_seeded
    _own_seeded.config.settings["search_starter_facts"] = True


def test_the_recent_feed_hides_seeded_memories(sv):
    """Unchanged behaviour, stated so the fix below cannot be read as a
    licence to bury real memories under thousands of starting ones."""
    out = sv.recent(caller="test", limit=5)
    assert not any(r["seeded"] for r in out["results"])
    assert out["counts"]["seeded"] == 6665


def test_the_panel_says_the_starting_memories_are_there():
    note = menubar.starter_note({"records": 6665, "organic": 0})
    assert "6,665" in note and "searchable" in note
    assert "nothing stored yet" != note


def test_the_panel_still_says_empty_when_it_really_is_empty():
    assert menubar.starter_note({"records": 0, "organic": 0}) == \
        "nothing stored yet"


def test_the_panel_counts_only_the_seeded_ones():
    note = menubar.starter_note({"records": 6670, "organic": 5})
    assert "6,665" in note


def test_the_tray_and_the_menu_bar_say_the_same_thing():
    from compartment import systray
    state = {"vault": "/v", "exists": True, "locked": False,
             "records": 6665, "organic": 0, "recent": [], "error": None,
             "settings": {"capture_hook": False, "search_starter_facts": True,
                          "auto_lock_minutes": 30}}
    rows = systray.panel_rows(state)
    empty = [text for kind, text in rows if kind == "empty"]
    assert empty and "6,665" in empty[0]


# ---------------------------------------------------------------- the toggle

def test_search_returns_starting_memories_by_default(sv):
    hits = sv.search("capital of France", caller="test", top_k=3)
    assert any("Paris" in h["text"] for h in hits["results"])


def test_turning_starter_facts_off_removes_them_from_search(sv):
    on = sv.search("capital of France", caller="test", top_k=5)["results"]
    assert any("Paris" in h["text"] for h in on)      # the toggle is what changed
    sv.config.settings["search_starter_facts"] = False
    off = sv.search("capital of France", caller="test", top_k=5)["results"]
    assert not any("Paris" in h["text"] for h in off)
    assert not any(is_seeded(json.dumps(h["tags"])) for h in off)


def test_turning_starter_facts_off_keeps_what_the_agent_stored(sv):
    sv.store("Max prefers the master-detail layout", caller="test")
    sv.config.settings["search_starter_facts"] = False
    hits = sv.search("master-detail layout preference", caller="test", top_k=5)
    texts = [h["text"] for h in hits["results"]]
    assert "Max prefers the master-detail layout" in [
        strip_provenance(t) for t in texts]
    assert not any(is_seeded(json.dumps(h["tags"])) for h in hits["results"])


def test_the_toggle_writes_the_setting_it_claims_to(tmp_path):
    """It used to write include_packs_in_search, which stopped meaning
    anything when the starting memories moved into "main"."""
    path = str(tmp_path / "v.vault")
    Vault.create(path, PASS, creator="t").lock()
    menubar.set_setting(path, "search_starter_facts", False)
    cfg = VaultConfig.load(path)
    assert cfg.settings["search_starter_facts"] is False
    assert cfg.settings.get("include_packs_in_search", True) is True


def test_the_toggle_reads_back_what_it_wrote(tmp_path):
    path = str(tmp_path / "v.vault")
    Vault.create(path, PASS, creator="t").lock()
    menubar.set_setting(path, "search_starter_facts", False)
    assert menubar.read_settings(path)["search_starter_facts"] is False
    menubar.set_setting(path, "search_starter_facts", True)
    assert menubar.read_settings(path)["search_starter_facts"] is True


def test_installed_packs_keep_their_own_switch(tmp_path):
    """Two different things, two different settings: the starter toggle must
    not silently change what installed packs do."""
    path = str(tmp_path / "v.vault")
    Vault.create(path, PASS, creator="t").lock()
    cfg = VaultConfig.load(path)
    cfg.settings["include_packs_in_search"] = False
    cfg.save(path)
    menubar.set_setting(path, "search_starter_facts", True)
    assert VaultConfig.load(path).settings["include_packs_in_search"] is False


# ------------------------------------------------------------------ the mark

def test_seeded_records_are_marked_by_their_id_tag(vault):
    packs.seed_records(vault, (DATA / "starter.mpack").read_bytes(), caller="t")
    row = vault.db.conn.execute(
        "SELECT tags FROM records LIMIT 1").fetchone()
    assert is_seeded(row["tags"])


def test_what_the_agent_stores_is_not_marked_seeded(vault):
    vault.store("something learned during use", caller="t")
    row = vault.db.conn.execute(
        "SELECT tags FROM records ORDER BY ikey DESC LIMIT 1").fetchone()
    assert not is_seeded(row["tags"])
    assert json.loads(row["tags"]) == []
