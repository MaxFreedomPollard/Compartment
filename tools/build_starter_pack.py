"""Build src/engram/data/starter.mpack from tools/starter/starter_facts.jsonl.

starter_facts.jsonl is THE canonical, hand-editable starter memory: one
JSON fact per line ({"id", "text", "tags"}). Edit it freely (insert,
delete, reword, append), then run this script. Every line is re-embedded
with the bundled model and the pack is re-signed, so edits become vector
memory automatically. Line position is irrelevant to retrieval; only ids
must stay unique (and core-001..core-260 must keep their texts, they are
the frozen `engram selftest` corpus).

Usage:  python tools/build_starter_pack.py [VERSION]
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from engram import packs
from engram.embed import DEFAULT_MODEL, Embedder

ROOT = pathlib.Path(__file__).resolve().parents[1]
IDENTITY_FILE = ROOT / "tools" / "pack_identity.json"
SRC = ROOT / "tools" / "starter" / "starter_facts.jsonl"
OUT = ROOT / "src" / "engram" / "data" / "starter.mpack"

VERSION = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"

records = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
ids = [r["id"] for r in records]
if len(set(ids)) != len(ids):
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    raise SystemExit(f"duplicate ids in starter_facts.jsonl: {dupes[:10]} - fix "
                     "before building")
missing = [i for i, r in enumerate(records, 1) if not r.get("text", "").strip()]
if missing:
    raise SystemExit(f"empty text at line(s) {missing[:10]} - fix before building")

identity = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
emb = Embedder(DEFAULT_MODEL)
print(f"embedding {len(records)} facts (bundled model, offline)…")
vectors = emb.embed_passages([r["text"] for r in records])
blob = packs.build_pack(
    name="starter", version=VERSION,
    description="engRAM starter knowledge: general facts (the frozen selftest "
                "corpus), pragmatic facts from the Artificial Knowledge "
                "Collection 6.0 (compilation CC BY-SA 4.0), and OS reference "
                "facts for macOS, Windows, and Linux.",
    records=records, vectors=vectors,
    model={"name": DEFAULT_MODEL, "sha256": emb.model_sha256, "dim": emb.dim},
    identity=identity)
OUT.write_bytes(blob)
print(f"built {OUT} ({len(records)} records, {len(blob)/1024/1024:.1f} MB, "
      f"v{VERSION})")
