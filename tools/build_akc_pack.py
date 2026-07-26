"""Regenerate the AKC-derived section of the starter memory (JSONL).

Converts AKC's structured records into self-contained natural-language fact
sentences (what a vector memory retrieves well), embeds them with the bundled
model, and signs the pack. Pragmatic subsets only - measurements, physical
constants, country facts, chemical elements, planets/moons/constellations,
and common-food nutrition. Pure facts, no opinions.

Source: MaxFreedomPollard/artificial-knowledge-collection-6.0 (compilation
CC BY-SA 4.0; components keep their own licenses). Point AKC_DIR at a local
checkout or let this script read the raw JSONL files placed alongside it.

Usage:  python tools/build_akc_pack.py [VERSION] [--akc-dir DIR]
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from compartment import packs
from compartment.embed import DEFAULT_MODEL, Embedder

ROOT = pathlib.Path(__file__).resolve().parents[1]
IDENTITY_FILE = ROOT / "tools" / "pack_identity.json"
OUT = ROOT / "tools" / "starter" / "akc_regenerated.jsonl"

# AKC source files (basename → subdir/file in a repo checkout)
AKC_FILES = {
    "measure": "measure-of-things/measure-of-things.jsonl",
    "constants": "physical-constants/constants.jsonl",
    "factbook": "world-factbook/world-factbook.jsonl",
    "features": "world-factbook/world-physical-features.jsonl",
    "sky": "sky-and-elements/sky-and-elements.jsonl",
    "nutrition": "nutrition/nutrition.jsonl",
}

# Keep nutrition pragmatic: common single-ingredient foods, capped.
NUTRITION_CAP = 600
COMMON_FOOD_GROUPS = {
    "Dairy and Egg Products", "Poultry Products", "Beef Products",
    "Pork Products", "Finfish and Shellfish Products", "Vegetables and Vegetable Products",
    "Fruits and Fruit Juices", "Cereal Grains and Pasta", "Legumes and Legume Products",
    "Nut and Seed Products", "Breakfast Cereals", "Baked Products",
}


def _num(x):
    if isinstance(x, float) and x.is_integer():
        return f"{int(x):,}"
    if isinstance(x, (int, float)):
        return f"{x:,}"
    return str(x)


def facts_measure(r):
    e, q, u = r.get("entity"), r.get("quantity"), r.get("unit", "")
    if not e or not q:
        return
    ctx = f" ({r['context']})" if r.get("context") else ""
    typ = r.get("typical")
    if typ is not None:
        lo, hi = r.get("low"), r.get("high")
        rng = f" (ranging from {_num(lo)} to {_num(hi)} {u})" if lo is not None and hi is not None else ""
        yield (f"The {q} of {'a ' if not e[0].isupper() else ''}{e}{ctx} is "
               f"typically about {_num(typ)} {u}{rng}.")


def facts_constants(r):
    name, val = r.get("name"), r.get("value_str") or r.get("value")
    if not name or val is None:
        return
    unit = f" {r['unit']}" if r.get("unit") else ""
    exact = " (an exact defined value)" if r.get("exact") else ""
    yield f"The physical constant '{name}' has the value {val}{unit}{exact}."


def facts_factbook(r):
    n = r.get("name")
    if not n:
        return
    if r.get("capital"):
        yield f"The capital of {n} is {r['capital']}."
    if r.get("population"):
        yield f"{n} has a population of about {_num(r['population'])} people."
    if r.get("area_sq_km"):
        yield f"{n} has a land area of about {_num(r['area_sq_km'])} square kilometers."
    if r.get("government_type"):
        yield f"The government of {n} is a {r['government_type']}."
    if r.get("languages"):
        langs = r["languages"].split("(")[0].strip().rstrip(",")
        yield f"The main language(s) of {n}: {langs}."
    if r.get("life_expectancy_years"):
        yield f"Life expectancy in {n} is about {r['life_expectancy_years']} years."
    if r.get("region"):
        yield f"{n} is a country in {r['region'].replace('_', ' ')}."


def facts_features(r):
    n, cat, k = r.get("name"), r.get("category"), r.get("kind")
    if n and cat:
        yield f"{n} is a {cat.lower()} ({k})."


def facts_sky(r):
    k = r.get("kind")
    if k == "element":
        n = r.get("name") or r.get("symbol")
        parts = []
        if r.get("symbol") and r.get("atomic_number"):
            parts.append(f"The chemical element {n} has symbol {r['symbol']} "
                         f"and atomic number {r['atomic_number']}.")
        if r.get("atomic_mass"):
            parts.append(f"{n} has a standard atomic weight of about {r['atomic_mass']}.")
        if r.get("standard_state"):
            parts.append(f"{n} is a {str(r['standard_state']).lower()} at room temperature.")
        if r.get("melting_point") and r.get("boiling_point"):
            parts.append(f"{n} melts at about {r['melting_point']} K and boils at "
                         f"about {r['boiling_point']} K.")
        yield from parts
    elif k == "planet":
        n = r.get("name")
        if n and r.get("mean_radius_km"):
            yield f"The planet {n} has a mean radius of about {_num(r['mean_radius_km'])} km."
        if n and r.get("orbital_period_days"):
            yield f"{n} orbits the Sun about every {_num(r['orbital_period_days'])} days."
    elif k == "moon" and r.get("name") and r.get("planet"):
        yield f"{r['name']} is a moon of {r['planet']}."
    elif k == "constellation" and r.get("name"):
        area = f" and covers about {_num(r['area_sq_deg'])} square degrees" if r.get("area_sq_deg") else ""
        yield f"{r['name']} is one of the 88 modern constellations{area}."


def facts_nutrition(r, counter):
    if counter[0] >= NUTRITION_CAP:
        return
    if r.get("food_group") not in COMMON_FOOD_GROUPS:
        return
    n = r.get("name")
    nut = r.get("nutrients_per_100g") or {}
    kcal = nut.get("Energy (kcal)")
    if not n or kcal is None:
        return
    prot = nut.get("Protein (g)")
    fat = nut.get("Total lipid (fat) (g)")
    extra = []
    if prot is not None:
        extra.append(f"{prot} g protein")
    if fat is not None:
        extra.append(f"{fat} g fat")
    tail = f" ({', '.join(extra)})" if extra else ""
    counter[0] += 1
    yield f"{n} contains about {_num(kcal)} kcal per 100 grams{tail}."


def load(akc_dir, key):
    d = pathlib.Path(akc_dir)
    rel = AKC_FILES[key]
    subdir, base = rel.split("/", 1)
    # Accept the repo layout (subdir/file) or flat staged files
    # (subdir_file, or just file).
    candidates = [d / rel, d / f"{subdir}_{base}", d / base]
    src = next((c for c in candidates if c.exists()), None)
    if src is None:
        print(f"  (skip {key}: none of {[str(c) for c in candidates]} found)")
        return []
    return [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    version = args[0] if args else "1.0.0"
    akc_dir = ROOT / "tools" / "akc_source"
    if "--akc-dir" in sys.argv:
        akc_dir = pathlib.Path(sys.argv[sys.argv.index("--akc-dir") + 1])

    texts, tags_by_i = [], []
    ncount = [0]
    plan = [
        ("measure", facts_measure, "measurements"),
        ("constants", facts_constants, "physics"),
        ("factbook", facts_factbook, "geography"),
        ("features", facts_features, "geography"),
        ("sky", facts_sky, "astronomy"),
    ]
    for key, fn, tag in plan:
        n0 = len(texts)
        for rec in load(akc_dir, key):
            for fact in fn(rec):
                texts.append(fact)
                tags_by_i.append(tag)
        print(f"  {key}: +{len(texts) - n0} facts")
    n0 = len(texts)
    for rec in load(akc_dir, "nutrition"):
        for fact in facts_nutrition(rec, ncount):
            texts.append(fact)
            tags_by_i.append("nutrition")
    print(f"  nutrition: +{len(texts) - n0} facts")

    records = [{"id": f"akc-{i+1:05d}", "text": t, "tags": ["akc", tags_by_i[i]]}
               for i, t in enumerate(texts)]
    print(f"total pragmatic facts: {len(records)}")
    if not records:
        raise SystemExit("no AKC facts produced - is tools/akc_source/ populated?")

    # The starter memory is unified now: this writes the regenerated AKC
    # section as JSONL. Merge it into tools/starter/starter_facts.jsonl
    # (replacing the akc-* lines), then run tools/build_starter_pack.py.
    with open(OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(records)} records) - merge into "
          "starter_facts.jsonl and rebuild with tools/build_starter_pack.py")


if __name__ == "__main__":
    main()
