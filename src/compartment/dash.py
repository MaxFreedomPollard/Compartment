"""Compartment dashboard: `compartment dash` → one local page showing what the vault
holds - how many memories, of what kind, growing how fast, connected how.

Security posture (same invariants as the rest of Compartment):
- Serves ENTIRELY from RAM. Nothing decrypted is ever written to disk;
  responses carry Cache-Control: no-store so the browser keeps them out of
  its disk cache too.
- Binds 127.0.0.1 only, on an ephemeral port, for exactly as long as the
  command runs. The URL contains a random token; requests without it get
  404 (constant-time compare), so other local processes cannot browse the
  vault by scanning ports.
- GET only; no endpoint writes to the vault. The panels read the in-RAM
  SQLite directly and the search box uses a no-record ranking pass, so
  looking at the vault does not touch a record, append an audit row, or
  reseal the file. The single exception is reopening after ANOTHER process
  wrote the vault: Vault.unlock compacts the replayed journal, which
  rewrites the file. That write belongs to the reopen, not to the page.
- Zero outbound connections, zero external assets - every byte of
  HTML/CSS/JS below ships in this file and the page's CSP forbids loading
  anything else.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .acl import AclError
from .crypto import CryptoError
from .ranking import CANDIDATE_POOL
from .vault import Vault, VaultLockedError

TAG_HIDE_PREFIX = "id:"          # seed ids would swamp the tag cloud

# Importance tiers (salience.py). The page's JS colours memory rows from this
# list (it is injected into PAGE below), so the tier boundaries and the hex
# values cannot drift apart.
TYPE_BUCKETS = [
    ("decisions & consent", 0.90, 1.01, "#e8b339"),
    ("personal facts & preferences", 0.80, 0.90, "#4fc3f7"),
    ("machine & configuration", 0.70, 0.80, "#a78bfa"),
    ("substantive statements", 0.25, 0.70, "#5fd38a"),
    ("pleasantries", -0.01, 0.25, "#8b98ad"),
]


class _VaultRef:
    """Hands out a live vault, transparently reopening when another process
    (the MCP server, the CLI) wrote the vault file since we read it."""

    def __init__(self, path: str, vault: Vault):
        self.path = path
        self._v = vault
        self._lock = threading.Lock()

    def get(self) -> Vault:
        with self._lock:
            if self._v is None or self._v._locked or self._v.is_stale():
                try:
                    pw, key = Vault.resolve_credential(self.path)
                except CryptoError as exc:      # no credential = still locked
                    raise VaultLockedError(str(exc)) from exc
                kf = None if key is not None else \
                    Vault.load_keyfile_hint(self.path)
                self._v = Vault.unlock(self.path, passphrase=pw, raw_key=key,
                                       keyfile=kf)
            return self._v


# ---------------------------------------------------------------- snapshots

def _ns_clause(allowed: set[str]) -> tuple[str, list[str]]:
    """SQL fragment + params limiting a query to the namespaces this caller
    may read, so the panels show exactly what the search box would return."""
    if not allowed:
        return "0", []
    return "ns IN (%s)" % ",".join("?" * len(allowed)), sorted(allowed)


def snapshot_stats(v: Vault, caller: str = "dash") -> dict:
    # The SQLite connection and the index are shared with whatever else is
    # using this vault, so every read runs under the vault's own lock.
    with v._oplock:
        v._require_open()
        allowed = set(v._readable_namespaces(caller))
        ns, nsp = _ns_clause(allowed)
        con = v.db.conn
        n_records = con.execute(
            f"SELECT COUNT(*) c FROM records WHERE {ns}", nsp).fetchone()["c"]
        n_relations = con.execute(
            f"SELECT COUNT(*) c FROM relations WHERE {ns}", nsp).fetchone()["c"]
        n_quar = con.execute(
            f"SELECT COUNT(*) c FROM records WHERE quarantined = 1 AND {ns}",
            nsp).fetchone()["c"]
        n_entities = con.execute(
            f"SELECT COUNT(*) c FROM (SELECT subject_n e FROM relations "
            f"WHERE {ns} UNION SELECT object_n FROM relations WHERE {ns})",
            nsp + nsp).fetchone()["c"]

        # Bucket in LOCAL time: every other timestamp on the page is rendered
        # with the browser's locale, and a UTC bucket would let the last bar
        # and the newest "recent memory" disagree by a day.
        growth = [{"d": r["d"], "n": r["n"]} for r in con.execute(
            f"SELECT date(created, 'unixepoch', 'localtime') d, COUNT(*) n "
            f"FROM records WHERE {ns} GROUP BY d ORDER BY d", nsp)]

        tags: dict[str, int] = {}
        agents: dict[str, int] = {}
        for row in con.execute(f"SELECT tags, prov FROM records WHERE {ns}",
                               nsp):
            for t in json.loads(row["tags"]):
                if not t.startswith(TAG_HIDE_PREFIX):
                    tags[t] = tags.get(t, 0) + 1
            agent = json.loads(row["prov"]).get("agent", "?")
            agents[agent] = agents.get(agent, 0) + 1
        top_tags = sorted(tags.items(), key=lambda kv: -kv[1])[:24]
        top_agents = sorted(agents.items(), key=lambda kv: -kv[1])

        preds: dict[str, int] = {}
        for row in con.execute(
                f"SELECT predicate FROM relations WHERE {ns}", nsp):
            preds[row["predicate"]] = preds.get(row["predicate"], 0) + 1
        top_preds = sorted(preds.items(), key=lambda kv: -kv[1])[:12]

        st = v.status()          # re-entrant: same thread already holds _oplock
    return {
        "vault": st["vault"], "vault_id": st["vault_id"],
        "records": n_records, "relations": n_relations,
        "entities": n_entities, "quarantined": n_quar,
        "namespaces": [e for e in st["namespaces"]
                       if e["namespace"] in allowed],
        "growth": growth,
        "tags": [{"tag": t, "count": c} for t, c in top_tags],
        "agents": [{"agent": a, "count": c} for a, c in top_agents],
        "predicates": [{"predicate": p, "count": c} for p, c in top_preds],
        "model": st["model"], "index": st["index"],
        "projected_ram_mb": st["projected_ram_mb"],
        "audit_ok": st["audit"]["ok"], "audit_entries": st["audit"]["entries"],
        "generated": time.time(),
    }


def snapshot_graph(v: Vault, caller: str = "dash",
                   max_edges: int = 400, max_nodes: int = 60) -> dict:
    # Every node is drawn as a labelled pill, so the graph shows the most
    # connected entities rather than every entity: a name has to fit on the
    # page to be worth drawing. total_entities lets the page say how many
    # it left out.
    # Vault.relations would append an audit row per page load; the store's
    # own query is the same data with no write. ACL is applied here instead.
    with v._oplock:
        v._require_open()
        allowed = set(v._readable_namespaces(caller))
        ns, nsp = _ns_clause(allowed)
        total = v.db.conn.execute(
            f"SELECT COUNT(*) c FROM relations WHERE {ns}", nsp).fetchone()["c"]
        total_entities = v.db.conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT subject_n e FROM relations "
            f"WHERE {ns} UNION SELECT object_n FROM relations WHERE {ns})",
            nsp + nsp).fetchone()["c"]
        rels = [{"subject": r["subject"], "predicate": r["predicate"],
                 "object": r["object"]}
                for r in v.db.query_relations(ns_in=allowed, limit=max_edges)]
    degree: dict[str, dict] = {}
    for r in rels:
        for name in (r["subject"], r["object"]):
            key = " ".join(name.split()).lower()
            d = degree.setdefault(key, {"id": key, "label": name, "degree": 0})
            d["degree"] += 1
    nodes = sorted(degree.values(), key=lambda n: -n["degree"])[:max_nodes]
    keep = {n["id"] for n in nodes}
    edges = []
    for r in rels:
        s = " ".join(r["subject"].split()).lower()
        o = " ".join(r["object"].split()).lower()
        if s in keep and o in keep:
            edges.append({"s": s, "o": o, "p": r["predicate"]})
    # total_relations is every relation the caller may read; the graph draws
    # at most max_edges of them, and only those between drawn entities.
    return {"nodes": nodes, "edges": edges, "total_relations": total,
            "shown_relations": len(edges), "total_entities": total_entities}


def snapshot_recent(v: Vault, caller: str = "dash", limit: int = 20) -> dict:
    out = []
    with v._oplock:
        v._require_open()
        allowed = set(v._readable_namespaces(caller))
        ns, nsp = _ns_clause(allowed)
        # Tombstones stay out of the dashboard's feed for the same reason
        # they stay out of Vault.recent: a superseded record has a live
        # replacement, and showing the replaced version presents a stance
        # its holder revised as current.
        for row in v.db.conn.execute(
                f"SELECT * FROM records WHERE superseded_by IS NULL "
                f"AND {ns} "
                f"ORDER BY created DESC LIMIT ?", nsp + [limit]):
            text = v.db.decrypt_text(row, v._master)
            out.append({
                "id": row["id"], "namespace": row["ns"],
                "text": text[:240] + ("…" if len(text) > 240 else ""),
                "importance": row["importance"], "created": row["created"],
                "quarantined": bool(row["quarantined"]),
                "tags": [t for t in json.loads(row["tags"])
                         if not t.startswith(TAG_HIDE_PREFIX)][:6],
            })
    return {"recent": out}


def snapshot_search(v: Vault, query: str, caller: str = "dash",
                    top_k: int = 10) -> dict:
    """Rank the same way Vault.search does, without recording the read.

    Vault.search charges the vault for every query: db.touch on each hit
    (which re-ranks the record by having been looked at) and one audit row
    per search. A dashboard that redraws itself would rewrite the vault's
    history simply by being open. So it borrows the vault's own scorer and
    skips only those two writes: the ranking here is the product's ranking,
    not a copy of it that can drift.
    """
    with v._oplock:
        v._require_open()
        allowed = set(v._readable_namespaces(caller))
        qvec = v.embedder.embed_query(query)
        boosted, vec_score, scores = v._rank_candidates(query, qvec,
                                                        CANDIDATE_POOL)

        results = []
        for rid in sorted(boosted, key=boosted.get, reverse=True):
            row = v.db.get_row(rid)
            if row is None or row["ns"] not in allowed:
                continue
            if row["superseded_by"]:      # same guard as Vault.search
                continue
            text = v.db.decrypt_text(row, v._master)
            results.append({
                "id": rid, "namespace": row["ns"], "text": text,
                "score": round(boosted[rid], 5),
                "cosine": round(vec_score.get(rid, 0.0), 4),
                "importance": row["importance"], "created": row["created"],
                "quarantined": bool(row["quarantined"]),
                "tags": [t for t in json.loads(row["tags"])
                         if not t.startswith(TAG_HIDE_PREFIX)][:6],
            })
            if len(results) >= top_k:
                break
    return {"results": results}


# ------------------------------------------------------------------ server

def _make_handler(ref: _VaultRef, token: str):
    token_b = token.encode("ascii")

    class DashHandler(BaseHTTPRequestHandler):
        server_version = "compartment-dash"
        _started = False              # has any byte of a response gone out?

        def log_message(self, *_):        # memory contents stay off the terminal
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self._started = True
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "img-src data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _fail(self, exc: Exception) -> None:
            """One error response, with a status a client can act on."""
            if self._started:
                return          # the response already went out (or died) - do
                                # not write a second one onto the same socket
            if isinstance(exc, VaultLockedError):
                code = 423      # locked: the user unlocks out of band
            elif isinstance(exc, AclError):
                code = 403
            elif isinstance(exc, CryptoError):
                code = 503      # stale reopen, tamper, model mismatch…
            else:
                code = 500
            try:
                self._json({"error": type(exc).__name__, "message": str(exc)},
                           code=code)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:          # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                try:
                    # http.server decodes the request line as latin-1, so a
                    # path with high bytes round-trips; compare bytes, and let
                    # anything that will not encode simply miss the token.
                    raw = parsed.path.encode("latin-1")
                except UnicodeEncodeError:
                    raw = b""
                head, _, tail = raw.strip(b"/").partition(b"/")
                if not hmac.compare_digest(head, token_b):
                    self._send(404, b"not found", "text/plain")
                    return
                route = tail.decode("latin-1")
                # The page itself needs no vault: a locked vault must still
                # render the page (which then reports the lock), not replace
                # it with a JSON blob.
                if route == "":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif route == "api/stats":
                    self._json(snapshot_stats(ref.get()))
                elif route == "api/graph":
                    self._json(snapshot_graph(ref.get()))
                elif route == "api/recent":
                    self._json(snapshot_recent(ref.get()))
                elif route == "api/search":
                    q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                    if not q.strip():
                        self._json({"results": []})
                    else:
                        self._json(snapshot_search(ref.get(), q))
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                return                     # browser navigated away mid-write
            except Exception as exc:       # locked vault, stale reopen failure…
                self._fail(exc)

    return DashHandler


def start(ref: _VaultRef, host: str = "127.0.0.1",
          port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Bind the dashboard and return (server, url). Caller runs serve_forever."""
    token = secrets.token_urlsafe(16)
    httpd = ThreadingHTTPServer((host, port), _make_handler(ref, token))
    httpd.daemon_threads = True
    url = f"http://{host}:{httpd.server_address[1]}/{token}/"
    return httpd, url


def run(path: str, vault: Vault) -> None:
    """CLI entry: serve until Ctrl-C. Zero flags, zero configuration."""
    import webbrowser
    ref = _VaultRef(path, vault)
    httpd, url = start(ref)
    print(f"Compartment dashboard: {url}")
    print("  serving from RAM, 127.0.0.1 only, GET only - viewing the vault "
          "changes nothing in it")
    print("  Ctrl-C to stop")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        httpd.server_close()


# ------------------------------------------------------------------- page

_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compartment</title>
<style>
:root{
 --bg:#0a0c11;--card:rgba(255,255,255,.028);--card2:rgba(255,255,255,.05);
 --line:rgba(255,255,255,.075);--line2:rgba(255,255,255,.14);
 --tx:#e9edf5;--dim:#939cb0;--faint:#5c6579;
 --blue:#58a6ff;--cyan:#7ee0ff;--gold:#e8b339;--purple:#a78bfa;
 --green:#5fd38a;--grey:#8b98ad;--red:#ff6b6b;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-color:#2a3040 transparent}
body{
 background:
  radial-gradient(1100px 500px at 12% -8%,rgba(66,133,244,.13),transparent 60%),
  radial-gradient(900px 420px at 95% 4%,rgba(232,179,57,.06),transparent 55%),
  radial-gradient(700px 700px at 50% 115%,rgba(88,166,255,.05),transparent 60%),
  var(--bg);
 color:var(--tx);min-height:100vh;
 font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
 -webkit-font-smoothing:antialiased;
 padding:26px clamp(14px,3.5vw,40px) 70px;max-width:1220px;margin:0 auto}
::selection{background:rgba(88,166,255,.35)}

/* ---------- header ---------- */
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
 margin-bottom:10px}
.brand{font-size:26px;font-weight:700;letter-spacing:.4px}
.brand .ac{background:linear-gradient(92deg,var(--blue),var(--cyan));
 -webkit-background-clip:text;background-clip:text;color:transparent}
.brand .dot{color:var(--gold)}
.path{font:11.5px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
 color:var(--dim);background:var(--card);border:1px solid var(--line);
 padding:7px 12px;border-radius:999px;max-width:52vw;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;
 padding:6px 11px;border-radius:999px;background:var(--card);
 border:1px solid var(--line);color:var(--dim)}
.badge i{width:6px;height:6px;border-radius:50%;background:var(--blue);
 box-shadow:0 0 8px var(--blue)}
.badge.ok i{background:var(--green);box-shadow:0 0 8px var(--green)}
.badge.bad{color:var(--red);border-color:rgba(255,107,107,.4)}
.badge.bad i{background:var(--red);box-shadow:0 0 8px var(--red)}
.tagline{width:100%;color:var(--faint);font-size:12.5px;margin:-2px 0 18px 2px}

/* ---------- stat tiles ---------- */
.tiles{display:grid;gap:12px;margin-bottom:16px;
 grid-template-columns:repeat(auto-fit,minmax(138px,1fr))}
.tile{position:relative;background:var(--card);border:1px solid var(--line);
 border-radius:16px;padding:16px 16px 13px;overflow:hidden;
 transition:transform .18s ease,border-color .18s ease}
.tile:hover{transform:translateY(-2px);border-color:var(--line2)}
.tile::before{content:"";position:absolute;inset:0 0 auto 0;height:1px;
 background:linear-gradient(90deg,transparent,var(--ac,#58a6ff88),transparent)}
.tile .n{font-size:26px;font-weight:700;letter-spacing:.3px;
 font-variant-numeric:tabular-nums;
 background:linear-gradient(180deg,#fff,#b9c6dd);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.tile .l{font-size:10.5px;color:var(--dim);text-transform:uppercase;
 letter-spacing:1.1px;margin-top:3px}
.tile .spark{position:absolute;right:12px;top:14px;width:8px;height:8px;
 border-radius:50%;background:var(--ac,#58a6ff);opacity:.85;
 box-shadow:0 0 10px var(--ac,#58a6ff)}

/* ---------- panels ---------- */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:880px){.grid{grid-template-columns:1fr}.path{max-width:100%}}
.panel{background:var(--card);border:1px solid var(--line);
 border-radius:18px;padding:20px 22px;min-width:0}
.panel.wide{grid-column:1/-1}
h2{display:flex;align-items:center;gap:9px;font-size:11.5px;font-weight:600;
 text-transform:uppercase;letter-spacing:1.6px;color:var(--dim);
 margin-bottom:14px}
h2::before{content:"";width:14px;height:3px;border-radius:2px;
 background:linear-gradient(90deg,var(--gold),transparent)}
h2 .sub{font-weight:400;letter-spacing:.3px;text-transform:none;
 color:var(--faint);margin-left:auto;font-size:11px}

/* ---------- composition ---------- */

/* ---------- charts ---------- */
canvas{width:100%;display:block}
.hint{color:var(--faint);font-size:11.5px;margin-top:10px}
.hint b{color:var(--dim);font-weight:600}
.err{color:var(--red)}

/* ---------- graph ---------- */
.graphwrap{position:relative}
#tip{position:absolute;pointer-events:none;display:none;z-index:2;
 background:rgba(16,20,28,.95);border:1px solid var(--line2);
 border-radius:10px;padding:7px 11px;font-size:12px;white-space:nowrap;
 line-height:1.5;box-shadow:0 8px 24px rgba(0,0,0,.5)}
#tip b{color:var(--cyan)}
#tip .d{color:var(--faint);font-size:11px}

/* ---------- tables & chips ---------- */
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:7px 8px;text-align:left;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
th{color:var(--faint);font-weight:500;font-size:10.5px;
 text-transform:uppercase;letter-spacing:1px}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}
tbody tr{transition:background .15s}
tbody tr:hover{background:rgba(255,255,255,.025)}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{background:var(--card);border:1px solid var(--line);
 border-radius:999px;padding:4px 12px;font-size:12px;color:var(--tx);
 transition:border-color .15s}
.chip:hover{border-color:var(--line2)}
.chip b{color:var(--blue);font-weight:600;margin-left:4px}
.chip.gold b{color:var(--gold)}

/* ---------- search ---------- */
.searchbox{position:relative;margin-bottom:6px}
.searchbox svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);
 width:15px;height:15px;stroke:var(--faint);fill:none;stroke-width:2}
#q{width:100%;background:var(--card2);color:var(--tx);
 border:1px solid var(--line);border-radius:14px;padding:12px 14px 12px 40px;
 font-size:14px;outline:none;transition:border-color .2s,box-shadow .2s}
#q::placeholder{color:var(--faint)}
#q:focus{border-color:rgba(88,166,255,.55);
 box-shadow:0 0 0 3px rgba(88,166,255,.14)}

/* ---------- memory rows / timeline ---------- */
.mem{position:relative;padding:10px 4px 10px 22px;font-size:13.2px;
 border-radius:10px;transition:background .15s}
.mem:hover{background:rgba(255,255,255,.025)}
.mem::before{content:"";position:absolute;left:7px;top:0;bottom:0;width:1px;
 background:var(--line)}
.mem .dot{position:absolute;left:3.5px;top:16px;width:8px;height:8px;
 border-radius:50%;background:var(--c,#8b98ad);box-shadow:0 0 8px var(--c)}
.mem .meta{color:var(--faint);font-size:11px;margin-top:3px}
.mem .meta .ns{color:var(--dim)}
.quar{color:var(--gold);font-size:11.5px}
footer{display:flex;justify-content:center;align-items:center;gap:8px;
 color:var(--faint);font-size:11px;margin-top:26px;letter-spacing:.3px}
footer i{width:5px;height:5px;border-radius:50%;background:var(--green);
 box-shadow:0 0 7px var(--green);display:inline-block}
</style></head><body>
<header>
 <div class="brand">Compart<span class="ac">ment</span><span class="dot">.</span></div>
 <span class="path" id="vaultpath">connecting…</span>
 <div class="meta">
  <span class="badge" id="modelbadge"><i></i><span>model</span></span>
  <span class="badge" id="indexbadge"><i></i><span>index</span></span>
  <span class="badge ok" id="auditbadge"><i></i><span>audit</span></span>
 </div>
</header>
<div class="tagline">everything your agents remember - encrypted, offline,
 served from RAM</div>
<div class="tiles" id="tiles"></div>
<div class="grid">
 <div class="panel wide">
  <h2>Memories over time<span class="sub" id="growthhint"></span></h2>
  <canvas id="growth" height="170"></canvas>
 </div>
 <div class="panel wide">
  <h2>Memory graph<span class="sub" id="graphcount"></span></h2>
  <div class="graphwrap">
   <canvas id="graph" height="400"></canvas>
   <div id="tip"></div>
  </div>
  <div class="hint" id="graphhint"></div>
 </div>
 <div class="panel">
  <h2>Namespaces</h2>
  <table id="nstable"><thead><tr><th>namespace</th><th
   style="text-align:right">memories</th></tr></thead><tbody></tbody></table>
 </div>
 <div class="panel">
  <h2>Written by</h2>
  <table id="agtable"><thead><tr><th>agent</th><th
   style="text-align:right">memories</th></tr></thead><tbody></tbody></table>
  <div style="height:16px"></div>
  <h2>Relation types</h2>
  <div class="chips" id="predchips"></div>
 </div>
 <div class="panel">
  <h2>Top tags</h2>
  <div class="chips" id="tagchips"></div>
 </div>
 <div class="panel">
  <h2>Search the vault</h2>
  <div class="searchbox">
   <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
   <input id="q" placeholder="ask memory anything…" autocomplete="off">
  </div>
  <div id="results"></div>
 </div>
 <div class="panel wide">
  <h2>Most recent memories</h2>
  <div id="recent"></div>
 </div>
</div>
<footer><i></i> served from RAM · 127.0.0.1 only · GET only, and viewing
 changes nothing · nothing decrypted touches disk · stop with Ctrl-C in the
 terminal</footer>
<script>
"use strict";
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=n=>n.toLocaleString("en-US");
const when=t=>new Date(t*1000).toLocaleString([], {dateStyle:"medium",timeStyle:"short"});
/* injected from TYPE_BUCKETS in dash.py: one source for tiers and colours */
const TYPE_TIERS=__TYPE_BUCKETS__;
const impColor=v=>{
 for(const t of TYPE_TIERS){if(v>=t.lo&&v<t.hi)return t.color;}
 return TYPE_TIERS[TYPE_TIERS.length-1].color;};
/* absolute, so /<token> works exactly like /<token>/ */
const API_BASE=location.pathname.replace(/\/+$/,"")+"/api/";
async function api(p){
 const r=await fetch(API_BASE+p);
 let j=null;try{j=await r.json();}catch(e){j=null;}
 if(j&&j.error)return j;
 if(!r.ok||j===null)return{error:"HTTP "+r.status,
  message:r.statusText||("HTTP "+r.status)};
 return j;}
function setBadge(id,text,cls){const b=$(id);
 b.querySelector("span").textContent=text;if(cls)b.className="badge "+cls;}

const TILE_AC={Memories:"#58a6ff",Relations:"#e8b339",Entities:"#7ee0ff",
 Namespaces:"#a78bfa","Quarantined":"#ff6b6b","Est. RAM":"#5fd38a",
 "Audit entries":"#8b98ad"};
function tiles(s){
 const t=[["Memories",s.records],["Relations",s.relations],
  ["Entities",s.entities],["Namespaces",s.namespaces.length],
  ["Quarantined",s.quarantined],["Est. RAM",s.projected_ram_mb+" MB"],
  ["Audit entries",s.audit_entries]];
 $("tiles").innerHTML=t.map(([l,n])=>
  `<div class="tile" style="--ac:${TILE_AC[l]}"><span class="spark"></span>`+
  `<div class="n">${typeof n==="number"?fmt(n):n}</div>`+
  `<div class="l">${l}</div></div>`).join("");
}
function growth(s){
 const c=$("growth"),dpr=devicePixelRatio||1;
 const W=c.clientWidth,H=170;c.width=W*dpr;c.height=H*dpr;
 c.style.height=H+"px";
 const x=c.getContext("2d");x.scale(dpr,dpr);
 const all=s.growth;if(!all.length)return;
 const L=34,R=10,B=24,T=12,IW=W-L-R,IH=H-T-B;
 x.strokeStyle="rgba(255,255,255,.05)";x.lineWidth=1;
 for(let g=0;g<4;g++){const gy=T+IH*g/3;
  x.beginPath();x.moveTo(L,gy);x.lineTo(W-R,gy);x.stroke();}
 /* one window, one x scale: bars and the cumulative line plot the same days */
 const days=all.slice(-60),off=all.length-days.length;
 let cum=0;const allCum=all.map(d=>cum+=d.n);
 const cums=allCum.slice(off);
 const maxN=Math.max(...days.map(d=>d.n),1);
 const slot=IW/days.length,bw=Math.min(34,Math.max(2.5,slot-3));
 const px=i=>L+i*slot+slot/2,py=i=>T+IH-IH*cums[i]/cum;
 days.forEach((d,i)=>{const h=Math.max(2,IH*d.n/maxN);
  const bx=px(i)-bw/2,by=T+IH-h;
  const gr=x.createLinearGradient(0,by,0,by+h);
  gr.addColorStop(0,"#58a6ff");gr.addColorStop(1,"#58a6ff33");
  x.fillStyle=gr;
  x.beginPath();x.roundRect(bx,by,bw,h,[3,3,0,0]);x.fill();});
 const area=x.createLinearGradient(0,T,0,T+IH);
 area.addColorStop(0,"rgba(232,179,57,.16)");area.addColorStop(1,"rgba(232,179,57,0)");
 x.beginPath();x.moveTo(px(0),T+IH);
 days.forEach((d,i)=>x.lineTo(px(i),py(i)));
 x.lineTo(px(days.length-1),T+IH);x.closePath();x.fillStyle=area;x.fill();
 x.strokeStyle="#e8b339";x.lineWidth=1.8;x.beginPath();
 days.forEach((d,i)=>{i?x.lineTo(px(i),py(i)):x.moveTo(px(i),py(i));});
 x.stroke();
 const lx=px(days.length-1),ly=py(days.length-1);
 x.beginPath();x.arc(lx,ly,3.4,0,7);x.fillStyle="#e8b339";
 x.shadowColor="#e8b339";x.shadowBlur=9;x.fill();x.shadowBlur=0;
 x.fillStyle="#5c6579";x.font="10px sans-serif";
 x.fillText(days[0].d,L,H-8);
 const lastLabel=days[days.length-1].d;
 x.fillText(lastLabel,W-R-x.measureText(lastLabel).width,H-8);
 $("growthhint").innerHTML=
  `<b style="color:#58a6ff">bars</b> per day · <b
   style="color:#e8b339">line</b> cumulative (${fmt(cum)})`+
  (off?` · last ${days.length} days with memories`:"");
}
function tablefill(id,rows){
 $(id).querySelector("tbody").innerHTML=rows.map(([a,b])=>
  `<tr><td>${esc(a)}</td><td class="num">${fmt(b)}</td></tr>`).join("");
}
function chips(id,items,k,v,cls){
 $(id).innerHTML=items.length?items.map(i=>
  `<span class="chip ${cls||""}">${esc(i[k])}<b>${fmt(i[v])}</b></span>`).join("")
  :'<span class="hint">none yet</span>';
}
function graph(g){
 /* Every entity is drawn as a pill with its name inside: no unlabeled
    shapes. Size and colour follow the number of relations. The server sends
    the most connected entities (see snapshot_graph), so the picture stays
    readable however large the vault. */
 const c=$("graph"),dpr=devicePixelRatio||1;
 const W=c.clientWidth,n=g.nodes.length;
 const H=Math.max(380,Math.min(760,120+n*10));
 c.width=W*dpr;c.height=H*dpr;c.style.height=H+"px";
 const x=c.getContext("2d");x.scale(dpr,dpr);
 const total=g.total_relations===undefined?g.edges.length:g.total_relations;
 const totalEnt=g.total_entities===undefined?n:g.total_entities;
 $("graphcount").textContent=n?
  (n<totalEnt?`${n} most connected of ${fmt(totalEnt)} entities`:`${n} entities`)+
  " · "+(g.edges.length<total
   ?`${fmt(g.edges.length)} of ${fmt(total)} relations drawn`
   :`${fmt(g.edges.length)} relations`):"";
 if(!n){
  $("graphhint").innerHTML="no relations mapped yet - the agent adds them "+
   "with <b>memory_link</b>, and you can add one yourself with "+
   "<b>compartment link \"Maya\" \"works at\" \"Acme\"</b>";
  return;}
 $("graphhint").innerHTML="<b>size and colour</b> = number of relations · hover a name to list its relations";
 const maxDeg=Math.max(...g.nodes.map(v=>v.degree),1);
 const trunc=s=>s.length>26?s.slice(0,25)+"…":s;
 const font=fs=>`600 ${fs}px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif`;
 const N=g.nodes.map((v,i)=>{
  const fs=Math.round(11+4*Math.sqrt(v.degree/maxDeg));
  x.font=font(fs);const label=trunc(v.label);
  const w=x.measureText(label).width+18,h=fs+12;
  return {...v,label,fs,w,h,r:Math.sqrt(w*w+h*h)/2,
   x:W/2+Math.cos(6.28*i/n)*Math.min(W,H)/3,
   y:H/2+Math.sin(6.28*i/n)*H/3,vx:0,vy:0};});
 const idx=Object.fromEntries(N.map((v,i)=>[v.id,i]));
 const E=g.edges.map(e=>({a:idx[e.s],b:idx[e.o],p:e.p}));
 const cap=v=>Math.max(-6,Math.min(6,v));
 const clampIn=v=>{v.x=Math.max(v.w/2+4,Math.min(W-v.w/2-4,v.x));
  v.y=Math.max(v.h/2+4,Math.min(H-v.h/2-4,v.y));};
 for(let it=0;it<360;it++){
  for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){
   const a=N[i],b=N[j];let dx=a.x-b.x,dy=a.y-b.y;
   const d=Math.sqrt(dx*dx+dy*dy)||1,min=a.r+b.r+10;
   let f=Math.min(4,2600/(d*d));
   if(d<min)f+=(min-d)*0.25;
   dx=dx/d*f;dy=dy/d*f;a.vx+=dx;a.vy+=dy;b.vx-=dx;b.vy-=dy;}
  E.forEach(e=>{const a=N[e.a],b=N[e.b];
   const dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1;
   const f=Math.max(-2.5,Math.min(2.5,(d-(a.r+b.r+70))*0.012));
   a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;});
  N.forEach(v=>{v.vx+=(W/2-v.x)*0.006;v.vy+=(H/2-v.y)*0.006;
   v.vx=cap(v.vx*0.6);v.vy=cap(v.vy*0.6);v.x+=v.vx;v.y+=v.vy;clampIn(v);});
 }
 /* pills are rectangles: a last pass separates any that still overlap */
 for(let pass=0;pass<16;pass++){
  let moved=false;
  for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){
   const a=N[i],b=N[j];
   const ox=(a.w+b.w)/2+6-Math.abs(a.x-b.x),oy=(a.h+b.h)/2+6-Math.abs(a.y-b.y);
   if(ox>0&&oy>0){moved=true;
    if(ox<oy){const s=(a.x<b.x?-1:1)*ox/2;a.x+=s;b.x-=s;}
    else{const s=(a.y<b.y?-1:1)*oy/2;a.y+=s;b.y-=s;}
    clampIn(a);clampIn(b);}}
  if(!moved)break;
 }
 const warm=deg=>{const t=Math.sqrt(deg/maxDeg);
  const r=Math.round(88+t*(232-88)),g2=Math.round(166+t*(179-166)),
        b=Math.round(255+t*(57-255));
  return `rgb(${r},${g2},${b})`;};
 function pill(v,col,alpha,hl){
  x.globalAlpha=alpha;
  const rr=v.h/2,X=v.x-v.w/2,Y=v.y-v.h/2;
  x.beginPath();x.moveTo(X+rr,Y);x.lineTo(X+v.w-rr,Y);
  x.arc(X+v.w-rr,Y+rr,rr,-Math.PI/2,Math.PI/2);x.lineTo(X+rr,Y+v.h);
  x.arc(X+rr,Y+rr,rr,Math.PI/2,3*Math.PI/2);x.closePath();
  x.fillStyle=col;x.shadowColor=col;x.shadowBlur=hl?16:0;x.fill();x.shadowBlur=0;
  if(hl){x.lineWidth=1.5;x.strokeStyle="#ffffff";x.stroke();}
  x.font=font(v.fs);x.textBaseline="middle";x.textAlign="center";
  x.fillStyle="#0b0f16";x.fillText(v.label,v.x,v.y+0.5);
  x.globalAlpha=1;
 }
 const linked=(i,j)=>E.some(e=>(e.a===i&&e.b===j)||(e.b===i&&e.a===j));
 function draw(hover){
  x.clearRect(0,0,W,H);
  E.forEach(e=>{const a=N[e.a],b=N[e.b];
   const on=hover>=0&&(e.a===hover||e.b===hover);
   const mx=(a.x+b.x)/2+(a.y-b.y)*0.12,my=(a.y+b.y)/2+(b.x-a.x)*0.12;
   x.strokeStyle=on?"rgba(126,224,255,.85)":"rgba(130,150,190,.32)";
   x.lineWidth=on?1.8:1;
   x.beginPath();x.moveTo(a.x,a.y);x.quadraticCurveTo(mx,my,b.x,b.y);x.stroke();});
  N.map((v,i)=>i).sort((i,j)=>N[i].degree-N[j].degree).forEach(i=>{
   const dimmed=hover>=0&&i!==hover&&!linked(hover,i);
   pill(N[i],warm(N[i].degree),dimmed?0.28:1,i===hover);});
 }
 draw(-1);
 const tip=$("tip");
 c.onmousemove=ev=>{const r=c.getBoundingClientRect();
  const mx=ev.clientX-r.left,my=ev.clientY-r.top;let best=-1;
  N.forEach((v,i)=>{if(Math.abs(v.x-mx)<=v.w/2+2&&Math.abs(v.y-my)<=v.h/2+2)best=i;});
  draw(best);
  if(best>=0){const v=N[best];
   const rel=E.filter(e=>e.a===best||e.b===best).slice(0,8).map(e=>
    e.a===best?`→ ${esc(e.p)} <b>${esc(N[e.b].label)}</b>`
              :`<b>${esc(N[e.a].label)}</b> ${esc(e.p)} →`);
   tip.style.display="block";
   tip.style.left=Math.min(v.x+v.w/2+8,W-260)+"px";
   tip.style.top=Math.max(4,v.y-v.h/2-8-18*(rel.length+1))+"px";
   tip.innerHTML=`<b>${esc(v.label)}</b> <span class="d">· ${v.degree} relation${v.degree===1?"":"s"}</span>`+
    (rel.length?`<div class="d">${rel.join("<br>")}</div>`:"");
  }else tip.style.display="none";};
 c.onmouseleave=()=>{draw(-1);tip.style.display="none";};
}
function memline(m){
 const col=impColor(m.importance);
 return `<div class="mem" style="--c:${col}"><span class="dot"></span>`+
  `${esc(m.text)}`+
  (m.quarantined?' <span class="quar">⚠ quarantined</span>':"")+
  `<div class="meta"><span class="ns">${esc(m.namespace)}</span> ·
   ${when(m.created)}`+
  (m.tags&&m.tags.length?` · ${m.tags.map(esc).join(", ")}`:"")+
  `</div></div>`;
}
let lastStats=null,lastGraph=null;
function redraw(){if(lastStats)growth(lastStats);if(lastGraph)graph(lastGraph);}
addEventListener("load",redraw);
/* the graph layout is O(N^2) x 300 iterations - never per resize event */
let rsz;
addEventListener("resize",()=>{clearTimeout(rsz);rsz=setTimeout(redraw,200);});
async function refresh(){
 try{
  const s=await api("stats");
  if(s.error){$("vaultpath").innerHTML=
   `<span class="err">${esc(s.message||s.error)}</span>`;return;}
  lastStats=s;
  $("vaultpath").textContent=s.vault;
  setBadge("modelbadge",s.model.name);
  setBadge("indexbadge",s.index);
  setBadge("auditbadge",s.audit_ok?"audit chain verified":"AUDIT CHAIN BROKEN",
   s.audit_ok?"badge ok":"badge bad");
  tiles(s);growth(s);
  tablefill("nstable",s.namespaces.map(n=>[n.namespace,n.records]));
  tablefill("agtable",s.agents.map(a=>[a.agent,a.count]));
  chips("tagchips",s.tags,"tag","count");
  chips("predchips",s.predicates,"predicate","count","gold");
  const rec=await api("recent");
  $("recent").innerHTML=(rec.recent||[]).map(memline).join("")||
   '<span class="hint">no memories yet</span>';
 }catch(e){$("vaultpath").innerHTML=`<span class="err">${esc(String(e))}</span>`;}
}
let deb;
$("q").addEventListener("input",()=>{clearTimeout(deb);
 deb=setTimeout(async()=>{
  const q=$("q").value.trim();
  if(!q){$("results").innerHTML="";return;}
  const r=await api("search?q="+encodeURIComponent(q));
  if(!r||r.error){$("results").innerHTML=
   `<div class="hint err">${esc((r&&(r.message||r.error))||"search failed")}</div>`;
   return;}
  $("results").innerHTML=(r.results||[]).map(m=>memline(
   {...m,quarantined:m.quarantined||false})).join("")||
   '<div class="hint">no matches</div>';},250);});
function graphError(msg){
 $("graphcount").textContent="";
 $("graphhint").innerHTML=`<span class="err">${esc(msg)}</span>`;}
refresh();
api("graph").then(g=>{
 if(!g||g.error||!g.nodes){
  graphError((g&&(g.message||g.error))||"graph unavailable");return;}
 lastGraph=g;graph(g);
}).catch(e=>graphError(String(e)));
setInterval(refresh,30000);
</script></body></html>
"""

# One definition of the importance tiers: the Python legend and the JS row
# colours read the same list, so a boundary or a hex cannot drift.
PAGE = _PAGE_TEMPLATE.replace("__TYPE_BUCKETS__", json.dumps(
    [{"label": label, "lo": lo, "hi": hi, "color": color}
     for label, lo, hi, color in TYPE_BUCKETS]))
