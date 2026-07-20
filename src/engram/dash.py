"""engRAM dashboard: `engram dash` → one local page showing what the vault
holds - how many memories, of what kind, growing how fast, connected how.

Security posture (same invariants as the rest of engRAM):
- Serves ENTIRELY from RAM. Nothing decrypted is ever written to disk;
  responses carry Cache-Control: no-store so the browser keeps them out of
  its disk cache too.
- Binds 127.0.0.1 only, on an ephemeral port, for exactly as long as the
  command runs. The URL contains a random token; requests without it get
  404 (constant-time compare), so other local processes cannot browse the
  vault by scanning ports.
- Read-only: GET only, no write endpoint exists. Zero outbound connections,
  zero external assets - every byte of HTML/CSS/JS below ships in this file
  and the page's CSP forbids loading anything else.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .vault import Vault

TAG_HIDE_PREFIX = "id:"          # seed ids would swamp the tag cloud

# Importance tiers (salience.py) → human buckets shown as "types of memories"
TYPE_BUCKETS = [
    ("decisions & consent", 0.90, 1.01, "#e8b339"),
    ("personal facts & preferences", 0.80, 0.90, "#4fc3f7"),
    ("machine & configuration", 0.70, 0.80, "#9575cd"),
    ("substantive statements", 0.25, 0.70, "#66bb6a"),
    ("pleasantries", -0.01, 0.25, "#78909c"),
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
                pw, key = Vault.resolve_credential(self.path)
                self._v = Vault.unlock(self.path, passphrase=pw, raw_key=key)
            return self._v


# ---------------------------------------------------------------- snapshots

def snapshot_stats(v: Vault) -> dict:
    con = v.db.conn
    n_records = v.db.count()
    n_relations = v.db.relation_count()
    n_quar = con.execute(
        "SELECT COUNT(*) c FROM records WHERE quarantined = 1").fetchone()["c"]
    n_entities = con.execute(
        "SELECT COUNT(*) c FROM (SELECT subject_n e FROM relations "
        "UNION SELECT object_n FROM relations)").fetchone()["c"]

    types = []
    for label, lo, hi, color in TYPE_BUCKETS:
        c = con.execute(
            "SELECT COUNT(*) c FROM records WHERE importance >= ? AND "
            "importance < ?", (lo, hi)).fetchone()["c"]
        types.append({"label": label, "count": c, "color": color})

    growth = [{"d": r["d"], "n": r["n"]} for r in con.execute(
        "SELECT date(created, 'unixepoch') d, COUNT(*) n FROM records "
        "GROUP BY d ORDER BY d")]

    tags: dict[str, int] = {}
    agents: dict[str, int] = {}
    for row in con.execute("SELECT tags, prov FROM records"):
        for t in json.loads(row["tags"]):
            if not t.startswith(TAG_HIDE_PREFIX):
                tags[t] = tags.get(t, 0) + 1
        agents[json.loads(row["prov"]).get("agent", "?")] = \
            agents.get(json.loads(row["prov"]).get("agent", "?"), 0) + 1
    top_tags = sorted(tags.items(), key=lambda kv: -kv[1])[:24]
    top_agents = sorted(agents.items(), key=lambda kv: -kv[1])

    preds: dict[str, int] = {}
    for row in con.execute("SELECT predicate FROM relations"):
        preds[row["predicate"]] = preds.get(row["predicate"], 0) + 1
    top_preds = sorted(preds.items(), key=lambda kv: -kv[1])[:12]

    st = v.status()
    return {
        "vault": st["vault"], "vault_id": st["vault_id"],
        "records": n_records, "relations": n_relations,
        "entities": n_entities, "quarantined": n_quar,
        "namespaces": st["namespaces"],
        "types": types, "growth": growth,
        "tags": [{"tag": t, "count": c} for t, c in top_tags],
        "agents": [{"agent": a, "count": c} for a, c in top_agents],
        "predicates": [{"predicate": p, "count": c} for p, c in top_preds],
        "model": st["model"], "index": st["index"],
        "projected_ram_mb": st["projected_ram_mb"],
        "audit_ok": st["audit"]["ok"], "audit_entries": st["audit"]["entries"],
        "generated": time.time(),
    }


def snapshot_graph(v: Vault, caller: str = "dash",
                   max_edges: int = 400, max_nodes: int = 120) -> dict:
    rels = v.relations(caller=caller, limit=max_edges)["relations"]
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
    return {"nodes": nodes, "edges": edges, "total_relations": len(rels)}


def snapshot_recent(v: Vault, limit: int = 20) -> dict:
    out = []
    for row in v.db.conn.execute(
            "SELECT * FROM records ORDER BY created DESC LIMIT ?", (limit,)):
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


# ------------------------------------------------------------------ server

def _make_handler(ref: _VaultRef, token: str):
    class DashHandler(BaseHTTPRequestHandler):
        server_version = "engram-dash"

        def log_message(self, *_):        # memory contents stay off the terminal
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
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

        def _json(self, obj: dict) -> None:
            self._send(200, json.dumps(obj).encode(), "application/json")

        def do_GET(self) -> None:          # noqa: N802 (http.server API)
            parsed = urllib.parse.urlparse(self.path)
            parts = parsed.path.strip("/").split("/", 1)
            if not parts or not hmac.compare_digest(parts[0], token):
                self._send(404, b"not found", "text/plain")
                return
            route = parts[1] if len(parts) > 1 else ""
            try:
                v = ref.get()
                if route == "":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif route == "api/stats":
                    self._json(snapshot_stats(v))
                elif route == "api/graph":
                    self._json(snapshot_graph(v))
                elif route == "api/recent":
                    self._json(snapshot_recent(v))
                elif route == "api/search":
                    q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                    if not q.strip():
                        self._json({"results": []})
                    else:
                        out = v.search(q, caller="dash", top_k=10)
                        self._json(out)
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as exc:       # locked vault, stale reopen failure…
                self._json({"error": type(exc).__name__, "message": str(exc)})

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
    print(f"engRAM dashboard: {url}")
    print("  serving from RAM, 127.0.0.1 only, read-only - Ctrl-C to stop")
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

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>engRAM</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d2129;--line:#262b36;
 --tx:#e6e9ef;--dim:#8b93a3;--acc:#58a6ff;--gold:#e8b339;--red:#ef5350}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 padding:20px 22px 60px;max-width:1180px;margin:0 auto}
h1{font-size:20px;letter-spacing:.3px}
h1 .ram{color:var(--acc)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:1.2px;
 color:var(--dim);margin:0 0 10px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:16px}
header .sub{color:var(--dim);font-size:12px}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2);
 border:1px solid var(--line);color:var(--dim)}
.badge.ok{color:#7bd88f;border-color:#2c4936}
.badge.bad{color:var(--red);border-color:#5a2a2a}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
 gap:10px;margin-bottom:18px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;
 padding:12px 14px}
.tile .n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .l{font-size:11px;color:var(--dim);text-transform:uppercase;
 letter-spacing:.8px;margin-top:2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
 padding:16px}
.panel.wide{grid-column:1/-1}
.typebar{display:flex;height:26px;border-radius:6px;overflow:hidden;
 margin-bottom:10px;background:var(--panel2)}
.typebar div{min-width:2px}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:12px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:3px;
 margin-right:6px;vertical-align:-1px}
.legend b{font-variant-numeric:tabular-nums}
canvas{width:100%;display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 8px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;
 letter-spacing:.8px}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:12px;
 padding:2px 10px;font-size:12px}
.chip b{color:var(--acc);font-weight:600}
.mem{padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.mem:last-child{border-bottom:none}
.mem .meta{color:var(--dim);font-size:11px;margin-top:2px}
.mem .imp{display:inline-block;width:34px;height:5px;border-radius:3px;
 background:var(--panel2);vertical-align:2px;margin-right:8px;overflow:hidden}
.mem .imp i{display:block;height:100%}
.quar{color:var(--gold)}
#q{width:100%;background:var(--panel2);color:var(--tx);
 border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:14px;
 outline:none}
#q:focus{border-color:var(--acc)}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
.err{color:var(--red)}
footer{color:var(--dim);font-size:11px;margin-top:22px;text-align:center}
</style></head><body>
<header>
 <h1>eng<span class="ram">RAM</span></h1>
 <span class="sub" id="vaultpath"></span>
 <span class="badge" id="modelbadge"></span>
 <span class="badge" id="indexbadge"></span>
 <span class="badge" id="auditbadge"></span>
</header>
<div class="tiles" id="tiles"></div>
<div class="grid">
 <div class="panel wide">
  <h2>What the vault remembers</h2>
  <div class="typebar" id="typebar"></div>
  <div class="legend" id="typelegend"></div>
 </div>
 <div class="panel wide">
  <h2>Memories over time</h2>
  <canvas id="growth" height="150"></canvas>
  <div class="hint" id="growthhint"></div>
 </div>
 <div class="panel wide">
  <h2>Memory graph <span id="graphcount" style="color:var(--dim)"></span></h2>
  <canvas id="graph" height="380"></canvas>
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
  <div style="height:14px"></div>
  <h2>Top relation types</h2>
  <div class="chips" id="predchips"></div>
 </div>
 <div class="panel">
  <h2>Top tags</h2>
  <div class="chips" id="tagchips"></div>
 </div>
 <div class="panel">
  <h2>Search the vault</h2>
  <input id="q" placeholder="ask memory anything…" autocomplete="off">
  <div id="results"></div>
 </div>
 <div class="panel wide">
  <h2>Most recent memories</h2>
  <div id="recent"></div>
 </div>
</div>
<footer>served from RAM · 127.0.0.1 only · read-only · nothing decrypted
 touches disk · close the terminal command to stop</footer>
<script>
"use strict";
const $=id=>document.getElementById(id);
const esc=s=>s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=n=>n.toLocaleString("en-US");
const when=t=>new Date(t*1000).toLocaleString();

async function api(p){const r=await fetch("api/"+p);return r.json();}

function tiles(s){
 const t=[["Memories",s.records],["Relations",s.relations],
  ["Entities",s.entities],["Namespaces",s.namespaces.length],
  ["Quarantined",s.quarantined],["Est. RAM",s.projected_ram_mb+" MB"],
  ["Audit entries",s.audit_entries]];
 $("tiles").innerHTML=t.map(([l,n])=>
  `<div class="tile"><div class="n">${typeof n==="number"?fmt(n):n}</div>`+
  `<div class="l">${l}</div></div>`).join("");
}
function types(s){
 const total=s.types.reduce((a,b)=>a+b.count,0)||1;
 $("typebar").innerHTML=s.types.map(t=>
  `<div style="background:${t.color};width:${100*t.count/total}%"
    title="${esc(t.label)}: ${fmt(t.count)}"></div>`).join("");
 $("typelegend").innerHTML=s.types.map(t=>
  `<span><span class="sw" style="background:${t.color}"></span>`+
  `${esc(t.label)} <b>${fmt(t.count)}</b>`+
  ` <span style="color:var(--dim)">(${(100*t.count/total).toFixed(1)}%)</span></span>`).join("");
}
function growth(s){
 const c=$("growth"),dpr=devicePixelRatio||1;
 const W=c.clientWidth,H=150;c.width=W*dpr;c.height=H*dpr;
 c.style.height=H+"px";
 const x=c.getContext("2d");x.scale(dpr,dpr);
 const days=s.growth;if(!days.length)return;
 const bars=days.slice(-60);
 const maxN=Math.max(...bars.map(d=>d.n),1);
 const slot=(W-40)/bars.length;
 const bw=Math.min(40,Math.max(2,slot-2));
 bars.forEach((d,i)=>{const h=(H-30)*d.n/maxN;
  x.fillStyle="#2e6db4";
  x.fillRect(20+i*slot+(slot-bw)/2,H-20-h,bw,h);});
 let cum=0;const cums=days.map(d=>cum+=d.n);
 x.strokeStyle="#e8b339";x.lineWidth=1.5;x.beginPath();
 days.forEach((d,i)=>{const px=20+(W-40)*i/(days.length-1||1),
  py=H-20-(H-34)*cums[i]/cum;i?x.lineTo(px,py):x.moveTo(px,py);});
 x.stroke();
 x.fillStyle="#8b93a3";x.font="10px sans-serif";
 x.fillText(bars[0].d,20,H-6);
 x.fillText(bars[bars.length-1].d,W-88,H-6);
 $("growthhint").textContent=
  `blue bars: memories per day (last ${bars.length} active days) · `+
  `gold line: cumulative total (${fmt(cum)})`;
}
function tablefill(id,rows){
 $(id).querySelector("tbody").innerHTML=rows.map(([a,b])=>
  `<tr><td>${esc(String(a))}</td><td class="num">${fmt(b)}</td></tr>`).join("");
}
function chips(id,items,k,v){
 $(id).innerHTML=items.length?items.map(i=>
  `<span class="chip">${esc(i[k])} <b>${fmt(i[v])}</b></span>`).join("")
  :'<span class="hint">none yet</span>';
}
function graph(g){
 const c=$("graph"),dpr=devicePixelRatio||1;
 const W=c.clientWidth,H=380;c.width=W*dpr;c.height=H*dpr;
 c.style.height=H+"px";
 const x=c.getContext("2d");x.scale(dpr,dpr);
 $("graphcount").textContent=g.nodes.length?
  `— ${g.nodes.length} entities, ${g.edges.length} relations`:"";
 if(!g.nodes.length){
  $("graphhint").innerHTML="no relations mapped yet — the agent can add them "+
   "with <b>memory_link</b>, or you can: <b>engram link \"Maya\" \"works at\" \"Acme\"</b>";
  return;}
 $("graphhint").textContent="node size = connectedness · hover a node for its name";
 const N=g.nodes.map((n,i)=>({...n,
  x:W/2+Math.cos(6.28*i/g.nodes.length)*(H/3),
  y:H/2+Math.sin(6.28*i/g.nodes.length)*(H/3),vx:0,vy:0}));
 const idx=Object.fromEntries(N.map((n,i)=>[n.id,i]));
 const E=g.edges.map(e=>({a:idx[e.s],b:idx[e.o],p:e.p}));
 const cap=v=>Math.max(-5,Math.min(5,v));
 for(let it=0;it<300;it++){
  for(let i=0;i<N.length;i++)for(let j=i+1;j<N.length;j++){
   const a=N[i],b=N[j];let dx=a.x-b.x,dy=a.y-b.y;
   const d=Math.sqrt(dx*dx+dy*dy)||1;
   const f=Math.min(3,900/(d*d));
   dx=dx/d*f;dy=dy/d*f;a.vx+=dx;a.vy+=dy;b.vx-=dx;b.vy-=dy;}
  E.forEach(e=>{const a=N[e.a],b=N[e.b];
   const dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1;
   const f=Math.max(-2,Math.min(2,(d-90)*0.01));
   a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;});
  N.forEach(n=>{n.vx+=(W/2-n.x)*0.008;n.vy+=(H/2-n.y)*0.008;
   n.vx=cap(n.vx*0.6);n.vy=cap(n.vy*0.6);
   n.x+=n.vx;n.y+=n.vy;
   n.x=Math.max(14,Math.min(W-14,n.x));
   n.y=Math.max(14,Math.min(H-14,n.y));});
 }
 function draw(hover){
  x.clearRect(0,0,W,H);
  x.strokeStyle="rgba(120,140,180,.28)";x.lineWidth=1;
  E.forEach(e=>{x.beginPath();x.moveTo(N[e.a].x,N[e.a].y);
   x.lineTo(N[e.b].x,N[e.b].y);x.stroke();});
  N.forEach((n,i)=>{const r=3+2.2*Math.sqrt(n.degree);
   x.beginPath();x.arc(n.x,n.y,r,0,6.29);
   x.fillStyle=i===hover?"#e8b339":"#58a6ff";x.fill();});
  const labeled=N.map((n,i)=>[n,i]).sort((a,b)=>b[0].degree-a[0].degree)
   .slice(0,18).map(p=>p[1]);
  x.font="11px sans-serif";
  labeled.concat(hover>=0&&!labeled.includes(hover)?[hover]:[]).forEach(i=>{
   const n=N[i];x.fillStyle=i===hover?"#e8b339":"#c6ccd8";
   x.fillText(n.label,n.x+8,n.y+3);});
 }
 draw(-1);
 c.onmousemove=ev=>{const r=c.getBoundingClientRect();
  const mx=ev.clientX-r.left,my=ev.clientY-r.top;let best=-1,bd=180;
  N.forEach((n,i)=>{const d=(n.x-mx)**2+(n.y-my)**2;
   if(d<bd){bd=d;best=i;}});
  draw(best);};
}
function impbar(v){
 const col=v>=0.9?"#e8b339":v>=0.8?"#4fc3f7":v>=0.7?"#9575cd":
  v>0.25?"#66bb6a":"#78909c";
 return `<span class="imp"><i style="width:${v*100}%;background:${col}"></i></span>`;
}
function memline(m){
 return `<div class="mem">${impbar(m.importance)}${esc(m.text)}`+
  (m.quarantined?' <span class="quar">⚠ quarantined</span>':"")+
  `<div class="meta">${esc(m.namespace)} · ${when(m.created)}`+
  (m.tags&&m.tags.length?` · ${m.tags.map(esc).join(", ")}`:"")+
  `</div></div>`;
}
let lastStats=null,lastGraph=null;
function redraw(){if(lastStats)growth(lastStats);if(lastGraph)graph(lastGraph);}
addEventListener("load",redraw);
addEventListener("resize",redraw);
async function refresh(){
 try{
  const s=await api("stats");
  lastStats=s;
  if(s.error){$("vaultpath").innerHTML=
   `<span class="err">${esc(s.message||s.error)}</span>`;return;}
  $("vaultpath").textContent=s.vault;
  $("modelbadge").textContent=s.model.name;
  $("indexbadge").textContent=s.index;
  const a=$("auditbadge");
  a.textContent=s.audit_ok?"audit chain ✓":"AUDIT CHAIN BROKEN";
  a.className="badge "+(s.audit_ok?"ok":"bad");
  tiles(s);types(s);growth(s);
  tablefill("nstable",s.namespaces.map(n=>[n.namespace,n.records]));
  tablefill("agtable",s.agents.map(a=>[a.agent,a.count]));
  chips("tagchips",s.tags,"tag","count");
  chips("predchips",s.predicates,"predicate","count");
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
  $("results").innerHTML=(r.results||[]).map(m=>memline(
   {...m,quarantined:m.quarantined||false})).join("")||
   '<div class="hint">no matches</div>';},250);});
refresh();
api("graph").then(g=>{lastGraph=g;graph(g);});
setInterval(refresh,30000);
</script></body></html>
"""
