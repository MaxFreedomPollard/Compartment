"""In-memory SQLite store (invariant I2: the database only ever exists in RAM).

The whole database image is serialized into the sealed vault payload.
Record text is additionally encrypted with a per-record key (crypto-shred);
FTS5 rows and freed pages are removed with DELETE + VACUUM on shred so the
serialized image genuinely no longer contains the content.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

import numpy as np

from . import crypto
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
    tags TEXT NOT NULL,             -- JSON list
    importance REAL NOT NULL,
    quarantined INTEGER NOT NULL,
    pack TEXT,                      -- pack name for pack records, NULL for organic
    prov TEXT NOT NULL,             -- JSON {host, agent, session}
    created REAL NOT NULL,
    accessed REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, text);
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
        if image is not None:
            self.conn.deserialize(image)
            # idempotent schema upgrade: vaults sealed by older versions gain
            # any new tables (e.g. relations) the moment they are opened
            self.conn.executescript(SCHEMA)
        else:
            self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("SELECT count(*) FROM fts")
        except sqlite3.OperationalError as exc:
            raise StoreError(
                "This Python's SQLite lacks FTS5, which engRAM requires "
                "for hybrid search. Install a Python built with full SQLite."
            ) from exc

    def serialize(self) -> bytes:
        return self.conn.serialize()

    # -- records ------------------------------------------------------------

    def next_ikey(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(ikey), 0) + 1 AS n FROM records").fetchone()
        return int(row["n"])

    def insert(self, *, record_id: str | None, ns: str, text: str, vec: np.ndarray,
               tags: list[str], importance: float, quarantined: bool, pack: str | None,
               prov: dict, master_key: bytes, created: float | None = None) -> str:
        rid = record_id or uuid.uuid4().hex
        rk, wrapped = crypto.new_record_key(master_key, rid)
        ct = crypto.seal(rk, crypto.canonical_json({"text": text}),
                         aad=b"engram-record-body:" + rid.encode())
        now = time.time()
        self.conn.execute(
            "INSERT INTO records (id, ikey, ns, ct, key_wrapped, vec, dim, tags, importance,"
            " quarantined, pack, prov, created, accessed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, self.next_ikey(), ns, ct, wrapped,
             vec.astype(np.float32).tobytes(), int(vec.shape[0]),
             json.dumps(tags), float(importance), int(quarantined), pack,
             json.dumps(prov), created or now, now),
        )
        self.conn.execute("INSERT INTO fts (id, text) VALUES (?, ?)", (rid, text))
        return rid

    def get_row(self, record_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()

    def decrypt_text(self, row: sqlite3.Row, master_key: bytes) -> str:
        rk = crypto.unwrap_record_key(master_key, row["id"], row["key_wrapped"])
        body = crypto.unseal(rk, row["ct"], aad=b"engram-record-body:" + row["id"].encode())
        return json.loads(body)["text"]

    def delete(self, record_id: str, shred: bool) -> bool:
        """Delete a record. shred=True also VACUUMs so the content (including
        FTS tokens and freed pages) is gone from the next serialized image,
        and the per-record key is destroyed with the row."""
        cur = self.conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        self.conn.execute("DELETE FROM fts WHERE id = ?", (record_id,))
        if cur.rowcount == 0:
            return False
        if shred:
            self.conn.execute("VACUUM")
        return True

    def touch(self, record_id: str) -> None:
        self.conn.execute("UPDATE records SET accessed = ? WHERE id = ?",
                          (time.time(), record_id))

    def all_vectors(self) -> tuple[list[str], list[int], np.ndarray]:
        rows = self.conn.execute("SELECT id, ikey, vec, dim FROM records ORDER BY ikey").fetchall()
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

    def fts_search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """BM25 keyword search. Returns (id, rank) best-first."""
        safe = " ".join(
            '"' + t.replace('"', "") + '"' for t in query.split() if t.replace('"', "")
        )
        if not safe:
            return []
        rows = self.conn.execute(
            "SELECT id, rank FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
            (safe, limit),
        ).fetchall()
        return [(r["id"], float(r["rank"])) for r in rows]

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
