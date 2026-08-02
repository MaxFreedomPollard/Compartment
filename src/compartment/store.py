"""In-memory SQLite store (invariant I2: the database only ever exists in RAM).

The whole database image is serialized into the sealed vault payload.
Record text is additionally encrypted with a per-record key (crypto-shred);
FTS5 rows and freed pages are removed with DELETE + VACUUM on shred so the
serialized image genuinely no longer contains the content.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid

import numpy as np

from . import crypto, wire
from .crypto import CryptoError

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    ikey INTEGER UNIQUE,            -- integer key for the vector index
    ns TEXT NOT NULL,
    ct BLOB NOT NULL,               -- AEAD(record_key, JSON{text})
    key_wrapped BLOB NOT NULL,      -- AEAD(master_key, record_key); destroyed on shred
    vec BLOB NOT NULL,              -- float32 embedding
    dim INTEGER NOT NULL,
    tags TEXT NOT NULL,             -- JSON list; mutable, retagging rewrites it
    importance REAL NOT NULL,
    quarantined INTEGER NOT NULL,
    pack TEXT,                      -- pack name for pack records, NULL for organic
    prov TEXT NOT NULL,             -- JSON {host, agent, session}
    created REAL NOT NULL,
    accessed REAL NOT NULL,
    -- How this fact came to be known, in a few words ("web search",
    -- "the user said so", "read from ~/.zshrc"). Kept OUT of the ciphertext
    -- and out of the embedding: a claim's provenance is metadata about the
    -- claim, and mixing it into the text makes every memory from one session
    -- look alike to the encoder. Rendered back into view on every read, so a
    -- reader never sees a bare assertion with no idea where it came from.
    source TEXT,
    -- The tags the memory was born with. `tags` drifts as the retagger learns
    -- what a memory turned out to relate to; this never moves, so "what did we
    -- think this was about at the time" stays answerable and a retagger bug
    -- can always be undone.
    tags_origin TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, text);
-- One row per EMBEDDING WINDOW. A record longer than the encoder's 512-token
-- input is embedded as several overlapping windows and scored by its best
-- one, so a long memory is searchable all the way through instead of only by
-- its opening. `records.vec` stays the first window, which is what a vault
-- written before this table contained, so an older image opens unchanged and
-- `all_vectors` falls back to it while this table is empty.
CREATE TABLE IF NOT EXISTS vecs (
    ikey INTEGER PRIMARY KEY,       -- index key, unique across every window
    id   TEXT NOT NULL,             -- the record this window belongs to
    seq  INTEGER NOT NULL,          -- window number within that record
    vec  BLOB NOT NULL,
    dim  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS vecs_by_id ON vecs(id);
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    subject_n TEXT NOT NULL,        -- normalized (lowercase) for matching
    predicate TEXT NOT NULL,
    predicate_n TEXT NOT NULL,
    object TEXT NOT NULL,
    object_n TEXT NOT NULL,
    ns TEXT NOT NULL,
    src_id TEXT,                    -- memory record this was derived from
    valid_from REAL,                -- when the fact became true (optional)
    valid_to REAL,                  -- when it stopped being true (optional)
    prov TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rel_subject ON relations(subject_n);
CREATE INDEX IF NOT EXISTS rel_object ON relations(object_n);
CREATE INDEX IF NOT EXISTS rel_predicate ON relations(predicate_n);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    caller TEXT NOT NULL,
    op TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class StoreError(CryptoError):
    pass


class Store:
    def __init__(self, image: bytes | None = None):
        # Autocommit: the DB lives only in RAM - durability comes from the
        # vault's own AEAD journal, and VACUUM (crypto-shred) needs no open tx.
        # check_same_thread=False: background writers (Hermes provider,
        # auto-lock) may touch the connection; Vault serializes all access
        # behind its operation lock.
        self.conn = sqlite3.connect(":memory:", isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Document frequencies are read once per term per session: they change
        # only when records are written, and a search asks for the same handful
        # of terms repeatedly while scoring.
        self._df_cache: dict[str, int] = {}
        if image is not None:
            self.conn.deserialize(image)
            # idempotent schema upgrade: vaults sealed by older versions gain
            # any new tables (e.g. relations) the moment they are opened
            self.conn.executescript(SCHEMA)
        else:
            self.conn.executescript(SCHEMA)
        self._migrate_columns()
        try:
            self.conn.execute("SELECT count(*) FROM fts")
        except sqlite3.OperationalError as exc:
            raise StoreError(
                "This Python's SQLite lacks FTS5, which Compartment requires "
                "for hybrid search. Install a Python built with full SQLite."
            ) from exc

    # New COLUMNS on an existing table, unlike new tables, do not arrive for
    # free: `CREATE TABLE IF NOT EXISTS` is a no-op the moment the table
    # exists, so a vault sealed by an older version would deserialize, skip the
    # whole statement, and then fail on the first query naming a new column.
    # Every entry here is nullable with no default, which is what lets an old
    # row stay valid without being rewritten: a memory stored before this
    # version simply has no recorded source, and reads as such.
    _ADDED_COLUMNS = (("source", "TEXT"), ("tags_origin", "TEXT"))

    def _migrate_columns(self) -> None:
        have = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(records)").fetchall()}
        for name, decl in self._ADDED_COLUMNS:
            if name not in have:
                self.conn.execute(
                    f"ALTER TABLE records ADD COLUMN {name} {decl}")

    def serialize(self) -> bytes:
        return self.conn.serialize()

    # -- records ------------------------------------------------------------

    def next_ikey(self) -> int:
        # Across BOTH tables: window keys and record keys share one space, so
        # a key can never be handed out twice and mean two different things.
        row = self.conn.execute(
            "SELECT COALESCE(MAX(k), 0) + 1 AS n FROM ("
            "  SELECT MAX(ikey) k FROM records UNION ALL SELECT MAX(ikey) FROM vecs)"
        ).fetchone()
        return int(row["n"])

    def set_vectors(self, record_id: str, vecs: np.ndarray) -> list[int]:
        """Replace every embedding window for one record. Returns the ikeys."""
        vecs = np.atleast_2d(np.asarray(vecs, dtype=np.float32))
        self.conn.execute("DELETE FROM vecs WHERE id = ?", (record_id,))
        keys = []
        for seq in range(vecs.shape[0]):
            k = self.next_ikey()
            self.conn.execute(
                "INSERT INTO vecs (ikey, id, seq, vec, dim) VALUES (?,?,?,?,?)",
                (k, record_id, seq, vecs[seq].tobytes(), int(vecs.shape[1])))
            keys.append(k)
        return keys

    def vector_keys(self, record_id: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT ikey FROM vecs WHERE id = ? ORDER BY seq", (record_id,)).fetchall()
        return [int(r["ikey"]) for r in rows]

    def vector_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT id, COUNT(*) c FROM vecs GROUP BY id").fetchall()
        return {r["id"]: r["c"] for r in rows}

    def insert(self, *, record_id: str | None, ns: str, text: str, vec: np.ndarray,
               tags: list[str], importance: float, quarantined: bool, pack: str | None,
               prov: dict, master_key: bytes, created: float | None = None,
               source: str | None = None) -> str:
        rid = record_id or uuid.uuid4().hex
        rk, wrapped = crypto.new_record_key(master_key, rid)
        ct = crypto.seal(rk, crypto.canonical_json({"text": text}),
                         aad=wire.record_body(rid)[0])
        now = time.time()
        # `vec` may be one window or several. The first is stored on the record
        # itself so the row keeps the shape every older vault has; all of them
        # go to `vecs`, which is what the index is built from.
        allv = np.atleast_2d(np.asarray(vec, dtype=np.float32))
        head = allv[0]
        self.conn.execute(
            "INSERT INTO records (id, ikey, ns, ct, key_wrapped, vec, dim, tags, importance,"
            " quarantined, pack, prov, created, accessed, source, tags_origin)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, self.next_ikey(), ns, ct, wrapped,
             head.tobytes(), int(head.shape[0]),
             json.dumps(tags), float(importance), int(quarantined), pack,
             json.dumps(prov), created or now, now,
             source, json.dumps(tags)),
        )
        self.set_vectors(rid, allv)
        self.conn.execute("INSERT INTO fts (id, text) VALUES (?, ?)", (rid, text))
        return rid

    def get_row(self, record_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()

    def decrypt_text(self, row: sqlite3.Row, master_key: bytes) -> str:
        rk = crypto.unwrap_record_key(master_key, row["id"], row["key_wrapped"])
        body = crypto.unseal_any(rk, row["ct"], *wire.record_body(row["id"]))
        return json.loads(body)["text"]

    def migrate_wire(self, master_key: bytes) -> int:
        """Rewrite every record under the current associated data.

        Records are the one place the labels do not migrate for free: a row's
        key wrap and ciphertext are written on insert and never touched again,
        so without this pass every read of every pre-2.2 record would pay a
        failed AEAD open before the one that works, forever. Vault.unlock
        calls this once and records it in the header.

        The record key is kept and re-wrapped rather than rotated, so the
        plaintext is the only thing that round-trips. Nothing is written to
        disk here: this mutates the in-memory database, and the caller's
        save() is what makes it real, atomically. If any row fails to decrypt
        the error propagates and no save happens, so a vault is never left
        half-converted."""
        rows = self.conn.execute("SELECT id, ct, key_wrapped FROM records").fetchall()
        for row in rows:
            rid = row["id"]
            rk = crypto.unwrap_record_key(master_key, rid, row["key_wrapped"])
            body = crypto.unseal_any(rk, row["ct"], *wire.record_body(rid))
            self.conn.execute(
                "UPDATE records SET ct = ?, key_wrapped = ? WHERE id = ?",
                (crypto.seal(rk, body, aad=wire.record_body(rid)[0]),
                 crypto.wrap_record_key(master_key, rid, rk), rid))
        return len(rows)

    def delete(self, record_id: str, shred: bool) -> bool:
        """Delete a record. shred=True also VACUUMs so the content (including
        FTS tokens and freed pages) is gone from the next serialized image,
        and the per-record key is destroyed with the row."""
        cur = self.conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        self.conn.execute("DELETE FROM fts WHERE id = ?", (record_id,))
        # Every embedding window goes with it. A window left behind would keep
        # answering searches for text the vault no longer holds, which is the
        # one thing forget() must never do.
        self.conn.execute("DELETE FROM vecs WHERE id = ?", (record_id,))
        if cur.rowcount == 0:
            return False
        if shred:
            self.conn.execute("VACUUM")
        return True

    def touch(self, record_id: str) -> None:
        self.conn.execute("UPDATE records SET accessed = ? WHERE id = ?",
                          (time.time(), record_id))

    # Tags carrying one of these prefixes are STRUCTURAL, not descriptive: they
    # are read by code, not by a person deciding what a memory is about. The
    # "id:" tag is how a seeded starting memory is told apart from one the agent
    # learned (vault.is_seeded), which drives what `memory_recent` shows and
    # whether the starter-facts search filter can find a record at all. A
    # retagger that dropped one would silently reclassify thousands of memories
    # and there would be nothing left to reconstruct the truth from, so they are
    # re-added on every write rather than trusted to survive.
    PROTECTED_TAG_PREFIXES = ("id:",)

    def protected_tags(self, record_id: str) -> list[str]:
        row = self.conn.execute(
            "SELECT tags FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return []
        return [t for t in json.loads(row["tags"])
                if t.startswith(self.PROTECTED_TAG_PREFIXES)]

    def set_tags(self, record_id: str, tags: list[str]) -> list[str]:
        """Replace a record's descriptive tags. Text, vectors, importance and
        `created` are untouched: this is the only mutation the retagger is
        allowed to make, and it cannot reach anything a reader would call the
        memory itself.

        Structural tags are merged back in whatever the caller passed, and the
        stored order is deduplicated and stable so an unchanged retag pass
        produces a byte-identical row and does not dirty the vault."""
        keep = self.protected_tags(record_id)
        seen, out = set(), []
        for t in list(keep) + [str(t).strip() for t in tags]:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        self.conn.execute("UPDATE records SET tags = ? WHERE id = ?",
                          (json.dumps(out), record_id))
        return out

    def all_vectors(self) -> tuple[list[str], list[int], np.ndarray]:
        """Every embedding window, as (record ids, index keys, matrix).

        `ids` is parallel to `ikeys` and repeats: several windows of one long
        record each map back to the same record.

        The two sources are UNIONED, never chosen between. A vault upgraded in
        place is partial by construction - records written since the upgrade
        carry window rows, every record written before it does not - so
        "use `vecs` if it has any rows, otherwise use `records`" silently drops
        every older memory out of the index. On a real vault that was 6,728 of
        6,839 memories, invisible to search while sitting safely in the file.
        Each record contributes its windows if it has them and its original
        single vector if it does not, and the two tables share one key space,
        so nothing can collide.
        """
        rows = list(self.conn.execute(
            "SELECT id, ikey, vec FROM vecs ORDER BY ikey").fetchall())
        windowed = {r["id"] for r in rows}
        rows += [r for r in self.conn.execute(
            "SELECT id, ikey, vec FROM records ORDER BY ikey").fetchall()
            if r["id"] not in windowed]
        ids = [r["id"] for r in rows]
        ikeys = [r["ikey"] for r in rows]
        if not rows:
            return ids, ikeys, np.zeros((0, 0), dtype=np.float32)
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
        return ids, ikeys, mat

    def count(self, ns: str | None = None) -> int:
        if ns is None:
            return self.conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
        return self.conn.execute("SELECT COUNT(*) c FROM records WHERE ns = ?", (ns,)).fetchone()["c"]

    def namespaces(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ns, COUNT(*) c FROM records GROUP BY ns ORDER BY ns").fetchall()
        return [{"namespace": r["ns"], "records": r["c"]} for r in rows]

    @staticmethod
    def _terms(query: str) -> list[str]:
        seen, out = set(), []
        for t in query.split():
            t = t.replace('"', "")
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    def _match(self, expr: str, limit: int) -> list[tuple[str, float]]:
        if not expr:
            return []
        try:
            rows = self.conn.execute(
                "SELECT id, rank FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (expr, limit)).fetchall()
        except sqlite3.OperationalError:      # unparseable query, never fatal
            return []
        return [(r["id"], float(r["rank"])) for r in rows]

    def doc_frequency(self, term: str) -> int:
        """How many records contain this term. Cached for the session."""
        key = term.lower()
        if key in self._df_cache:
            return self._df_cache[key]
        try:
            n = self.conn.execute(
                "SELECT count(*) c FROM fts WHERE fts MATCH ?",
                ('"' + term.replace('"', "") + '"',)).fetchone()["c"]
        except sqlite3.OperationalError:
            n = 0
        self._df_cache[key] = n
        return n

    def fts_search(self, query: str, limit: int,
                   common_fraction: float = 0.10) -> list[tuple[str, float]]:
        """BM25 keyword search, best-first. Returns (id, rank); rank is -bm25.

        FTS5's default operator is implicit AND, so a nine-word question had to
        appear in a record word for word or the keyword channel returned
        nothing at all - which is to say it went silent on exactly the long,
        specific questions an agent actually asks.

        Falling back to plain OR trades that for the opposite failure: "how",
        "the" and "what" match a large share of the vault and the results fill
        with records sharing nothing but function words. So the fallback ORs
        only the terms that carry information, dropping any that appear in more
        than `common_fraction` of records. That ceiling is measured from this
        vault rather than taken from an English stopword list, so it behaves
        the same for a vault full of code, names, or another language.
        """
        terms = self._terms(query)
        if not terms:
            return []
        hits = self._match(" ".join(f'"{t}"' for t in terms), limit)
        if hits:
            return hits
        total = self.count()
        ceiling = max(1, int(total * common_fraction))
        keep = [t for t in terms if self.doc_frequency(t) <= ceiling] or terms
        return self._match(" OR ".join(f'"{t}"' for t in keep), limit)

    def term_information(self, query: str) -> dict[str, float]:
        """Self-information log(N/(1+df)) per query term, in nats.

        This is what makes a literal match comparable to a semantic one: it
        measures how surprising the match is, not how strong it looks.
        """
        total = max(1, self.count())
        info = {}
        for t in self._terms(query):
            df = self.doc_frequency(t)
            if df > 0:
                info[t] = math.log(total / (1.0 + df))
        return info

    # -- relations (the memory graph) ---------------------------------------

    @staticmethod
    def _norm_entity(s: str) -> str:
        return " ".join((s or "").split()).lower()

    def insert_relation(self, *, rel_id: str | None, subject: str, predicate: str,
                        obj: str, ns: str, src_id: str | None,
                        valid_from: float | None, valid_to: float | None,
                        prov: dict, created: float | None = None) -> str:
        rid = rel_id or uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO relations (id, subject, subject_n, predicate, predicate_n,"
            " object, object_n, ns, src_id, valid_from, valid_to, prov, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, subject.strip(), self._norm_entity(subject),
             predicate.strip(), self._norm_entity(predicate),
             obj.strip(), self._norm_entity(obj), ns, src_id,
             valid_from, valid_to, json.dumps(prov), created or time.time()))
        return rid

    def find_relation(self, subject: str, predicate: str, obj: str,
                      ns: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM relations WHERE subject_n = ? AND predicate_n = ?"
            " AND object_n = ? AND ns = ?",
            (self._norm_entity(subject), self._norm_entity(predicate),
             self._norm_entity(obj), ns)).fetchone()

    def get_relation(self, rel_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM relations WHERE id = ?", (rel_id,)).fetchone()

    def delete_relation(self, rel_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM relations WHERE id = ?", (rel_id,))
        return cur.rowcount > 0

    def query_relations(self, *, entity: str | None = None,
                        subject: str | None = None, predicate: str | None = None,
                        obj: str | None = None, as_of: float | None = None,
                        ns_in: set[str] | None = None,
                        limit: int = 500) -> list[sqlite3.Row]:
        """Deterministic filter over the graph; any combination of criteria.
        `entity` matches subject OR object. `as_of` keeps relations whose
        validity window covers that instant (open-ended windows always match).
        """
        where, params = [], []
        if entity is not None:
            where.append("(subject_n = ? OR object_n = ?)")
            params += [self._norm_entity(entity)] * 2
        if subject is not None:
            where.append("subject_n = ?")
            params.append(self._norm_entity(subject))
        if predicate is not None:
            where.append("predicate_n = ?")
            params.append(self._norm_entity(predicate))
        if obj is not None:
            where.append("object_n = ?")
            params.append(self._norm_entity(obj))
        if as_of is not None:
            where.append("(valid_from IS NULL OR valid_from <= ?)")
            params.append(as_of)
            where.append("(valid_to IS NULL OR valid_to >= ?)")
            params.append(as_of)
        if ns_in is not None:
            if not ns_in:
                return []
            where.append(f"ns IN ({','.join('?' * len(ns_in))})")
            params += sorted(ns_in)
        sql = "SELECT * FROM relations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created DESC, id LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def relation_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM relations").fetchone()["c"]

    def entity_degrees(self, ns_in: set[str] | None = None,
                       limit: int = 200) -> list[dict]:
        """Entities ranked by how many relations touch them (display casing =
        most recent spelling seen)."""
        rows = self.query_relations(ns_in=ns_in, limit=100_000)
        seen: dict[str, dict] = {}
        for r in rows:
            for norm, disp in ((r["subject_n"], r["subject"]),
                               (r["object_n"], r["object"])):
                e = seen.setdefault(norm, {"entity": disp, "degree": 0})
                e["degree"] += 1
        out = sorted(seen.values(), key=lambda e: -e["degree"])
        return out[:limit]

    # -- meta ---------------------------------------------------------------

    def get_meta(self, k: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
