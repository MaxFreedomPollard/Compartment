"""The shipped starter pack: present, signed, precomputed-vector, complete."""
import pathlib

from engram import packs

DATA = pathlib.Path(__file__).resolve().parents[1] / "src" / "engram" / "data"


def test_starter_pack_ships_and_verifies():
    blob = (DATA / "starter.mpack").read_bytes()
    header, records, vectors = packs.read_pack(blob)  # verifies sig+hash
    assert len(records) == vectors.shape[0] == header["records"] == 4808
    assert vectors.shape[1] == 384


def test_starter_sections_all_present():
    _, records, _ = packs.read_pack((DATA / "starter.mpack").read_bytes())
    prefixes = {r["id"].split("-")[0] for r in records}
    assert {"core", "akc", "macos", "windows", "linux"} <= prefixes
    core = [r for r in records if r["id"].startswith("core-")]
    assert len(core) == 260  # frozen selftest corpus intact


def test_starter_source_matches_pack():
    """tools/starter/starter_facts.jsonl is canonical; the built pack must
    match it line for line."""
    import json
    src = pathlib.Path(__file__).resolve().parents[1] / "tools" / "starter" / "starter_facts.jsonl"
    source = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    _, records, _ = packs.read_pack((DATA / "starter.mpack").read_bytes())
    assert [r["id"] for r in records] == [r["id"] for r in source]
    assert [r["text"] for r in records] == [r["text"] for r in source]


def test_os_facts_recallable(vault):
    packs.seed_records(vault, (DATA / "starter.mpack").read_bytes(), caller="t")
    hit = vault.search("registry hive for machine-wide settings", caller="t",
                       top_k=3)["results"]
    assert any("HKEY_LOCAL_MACHINE" in h["text"] or "HKLM" in h["text"] for h in hit)
    hit = vault.search("command to sign code on mac", caller="t",
                       top_k=3)["results"]
    assert any("codesign" in h["text"] for h in hit)


def test_seed_lands_in_main_fully_editable(vault):
    """Starting memories are ordinary memories: in "main", no pack marker,
    forgettable, and sitting in the same namespace new memories go to."""
    out = packs.seed_records(vault, (DATA / "starter.mpack").read_bytes(),
                             caller="t")
    assert out["namespace"] == "main"
    assert out["used_precomputed_vectors"] is True
    assert vault.db.count("main") == 4808
    assert vault.db.count("packs/starter") == 0
    assert vault.pack_list() == []          # no separate section registered
    row = vault.db.conn.execute(
        "SELECT id, pack FROM records LIMIT 1").fetchone()
    assert row["pack"] is None
    # editable: forget one starting memory like any other memory
    vault.forget(row["id"], caller="t")
    assert vault.db.count("main") == 4807
    # new memories land in the very same namespace
    vault.store("organic memory next to the starters", caller="t")
    assert vault.db.count("main") == 4808


def test_legacy_starter_vault_merges_on_unlock(tmp_path):
    """A vault built by an older engRAM (separate read-only packs/starter
    section) is reorganized on unlock: every record moves to "main" with
    text, tags, importance, created timestamps and provenance untouched.
    Nothing is deleted, nothing is re-embedded."""
    from conftest import PASS
    from engram.vault import Vault
    p = str(tmp_path / "legacy.vault")
    v, _ = Vault.create(p, PASS, creator="legacy")
    packs.install_pack(v, (DATA / "starter.mpack").read_bytes(), caller="legacy")
    before = {
        r["id"]: (r["tags"], r["importance"], r["created"], r["prov"], r["vec"])
        for r in v.db.conn.execute("SELECT * FROM records")}
    assert v.db.count("packs/starter") == 4808
    v.lock()

    v2 = Vault.unlock(p, passphrase=PASS)
    assert v2.db.count("packs/starter") == 0
    assert v2.db.count("main") == 4808
    assert v2.pack_list() == []
    after = {
        r["id"]: (r["tags"], r["importance"], r["created"], r["prov"], r["vec"])
        for r in v2.db.conn.execute("SELECT * FROM records")}
    assert after == before                   # pure reorganization, zero edits
    # merged memories are freely editable now
    rid = next(iter(after))
    v2.forget(rid, caller="legacy")
    assert v2.db.count("main") == 4807
    # audit trail records the reorganization
    ops = [r["op"] for r in v2.db.conn.execute("SELECT op FROM audit")]
    assert "merge" in ops
    # and the merge is durable: reopen shows the merged layout
    v2.lock()
    v3 = Vault.unlock(p, passphrase=PASS)
    assert v3.db.count("main") == 4807 and v3.db.count("packs/starter") == 0


def test_pack_export_roundtrip(tmp_path):
    """export → hand-edit (insert a line mid-file) → rebuild keeps everything."""
    import json as _json
    from engram.embed import DEFAULT_MODEL, Embedder
    _, records, _ = packs.read_pack((DATA / "starter.mpack").read_bytes())
    sample = records[:40]  # keep the test fast
    lines = [_json.dumps(r, sort_keys=True) for r in sample]
    lines.insert(20, _json.dumps({"id": "custom-0001",
                                  "text": "Hand-injected mid-file fact.",
                                  "tags": ["custom"]}))
    edited = [_json.loads(l) for l in lines]
    ident = packs.new_identity("editor")
    emb = Embedder(DEFAULT_MODEL)
    blob2 = packs.build_pack(
        name="starter", version="1.0.1", description="edited",
        records=edited, vectors=emb.embed_passages([r["text"] for r in edited]),
        model={"name": DEFAULT_MODEL, "sha256": emb.model_sha256, "dim": emb.dim},
        identity=ident)
    h2, r2, _ = packs.read_pack(blob2)
    assert h2["records"] == 41 and r2[20]["id"] == "custom-0001"


def test_bundled_hermes_plugin_in_sync():
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in ("__init__.py", "plugin.yaml"):
        canonical = (root / "integrations" / "hermes" / "engram" / f).read_bytes()
        shipped = (root / "src" / "engram" / "data" / "hermes-plugin" / f).read_bytes()
        assert canonical == shipped, f"{f} out of sync - re-copy into data/hermes-plugin"
