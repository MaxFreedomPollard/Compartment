"""engRAM CLI. Fail-fast, menu-driven where interactive, flag-driven for scripts.

`serve` runs the MCP stdio server. `setup download-model` is the ONLY
network-capable operation in the product; everything else is offline forever.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

from . import __version__, audit, offline_guard, packs, selftest, session
from .acl import VaultConfig
from .crypto import CryptoError
from .embed import DEFAULT_MODEL, OPTIONAL_MODELS, Embedder, user_model_dir
from .vault import Vault, keychain_clear, keychain_get, keychain_store
from .vaultfile import read_vault_file, verify_manifest

DEFAULT_VAULT = os.environ.get("ENGRAM_VAULT",
                               str(Path.home() / ".engram" / "memory.vault"))


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _open_vault(args) -> Vault:
    try:
        pw, key = Vault.resolve_credential(args.vault)
    except CryptoError:
        pw = getpass.getpass(f"Passphrase for {args.vault}: ")
        key = None
    return Vault.unlock(args.vault, passphrase=pw, raw_key=key)


def _data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def _pack_bytes(name: str) -> bytes | None:
    p = _data_dir() / f"{name}.mpack"
    return p.read_bytes() if p.is_file() else None


def _seed_pack_bytes() -> bytes:
    b = _pack_bytes("starter")
    if b is None:
        raise CryptoError("Bundled starter.mpack is missing from this install")
    return b


def _starter_pack_names() -> list[str]:
    """Seeded at init as ordinary editable memories in "main" (general
    facts + AKC pragmatic knowledge + macOS/Windows/Linux references).
    The .mpack is only the signed delivery container - there is no
    separate starter section in the vault."""
    return ["starter"]


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------- commands

def cmd_init(args) -> None:
    path = args.vault
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(path):
        _die(f"{path} already exists - engRAM never overwrites a vault")
    print(f"engRAM {__version__} - creating vault: {path}")
    print(f"Embedding model: {DEFAULT_MODEL} (bundled, offline)")
    if args.passphrase:
        pw = args.passphrase
    else:
        pw = getpass.getpass("Choose a passphrase: ")
        pw2 = getpass.getpass("Repeat passphrase:  ")
        if pw != pw2:
            _die("passphrases do not match")
    if not pw:
        _die("empty passphrase refused")
    v, words = Vault.create(path, pw, creator=args.creator)
    print("\n=== RECOVERY PHRASE (shown exactly once - write it down) ===")
    for i in range(0, 16, 4):
        print("   " + "  ".join(f"{j+1:2d}.{words[j]}" for j in range(i, i + 4)))
    print("=" * 60)
    print("\nFinishing vault setup (offline)…")
    total = 0
    for name in _starter_pack_names():
        blob = _pack_bytes(name)
        if blob is None:
            continue
        out = packs.seed_records(v, blob, caller=args.creator)
        total += out["records"]
        print(f"  {out['name']}@{out['version']}: {out['records']} starting "
              "memories")
    print(f"  → vault ready ({total} memories in 'main' - editable and "
          "forgettable like anything the agent stores)")
    if args.keychain:
        if sys.platform != "darwin":
            _die("--keychain is only available on macOS")
        keychain_store(path, v._master)
        print("  keychain credential stored (persists across reboots)")
    elif not args.no_session:
        session.store(path, v._master)
        print("  unlocked: stays open until the next restart/power loss or "
              "`engram lock`")
    st = v.status()
    v.save()
    print(f"\nVault ready: {st['records']} records, projected RAM "
          f"~{st['projected_ram_mb']}MB. Run `engram selftest` to verify.")


def _ask_yn(q: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{q} [y/N] ").strip().lower().startswith("y")


def cmd_unlock(args) -> None:
    pw = args.passphrase or getpass.getpass("Passphrase (or recovery phrase): ")
    v = Vault.unlock(args.vault, passphrase=pw)   # verifies credential
    if args.keychain:
        if sys.platform != "darwin":
            _die("--keychain is only available on macOS")
        keychain_store(args.vault, v._master)
        print("unlocked: KEYCHAIN credential stored - persists across reboots "
              "until `engram lock` (see SECURITY.md for the tradeoff)")
    elif args.once:
        print("credential verified for this invocation only (no credential stored)")
    else:
        session.store(args.vault, v._master)
        print("unlocked: stays unlocked continuously - through logins, for "
              "weeks or months - until the next RESTART/power loss or "
              "`engram lock`.")
    v.save()


def cmd_lock(args) -> None:
    if args.sign:
        v = _open_vault(args)
        ident_path = Path(args.identity)
        if ident_path.exists():
            identity = json.loads(ident_path.read_text())
        else:
            identity = packs.new_identity(args.creator)
            ident_path.parent.mkdir(parents=True, exist_ok=True)
            ident_path.write_text(json.dumps(identity, indent=2))
            print(f"generated signing identity → {ident_path} (keep it private)")
        v.lock(signing_key=packs.load_signing_key(identity))
        print(f"vault sealed + signed by {identity['signer']} "
              f"(pub {identity['pub_hex'][:16]}…); verify with `engram verify`")
    cleared_session = session.clear(args.vault)
    cleared_kc = keychain_clear(args.vault)
    what = [n for n, c in (("session", cleared_session), ("keychain", cleared_kc)) if c]
    print(f"locked: cleared {' + '.join(what) if what else 'no'} stored "
          "credential(s). The vault file is sealed at rest; nothing can open "
          "it without the passphrase.")


def cmd_status(args) -> None:
    if not os.path.exists(args.vault):
        _die(f"no vault at {args.vault} (run `engram init`)")
    try:
        pw, key = Vault.resolve_credential(args.vault)
        v = Vault.unlock(args.vault, passphrase=pw, raw_key=key)
        _print(v.status())
    except CryptoError:
        loaded = read_vault_file(args.vault)
        _print({"vault": args.vault, "locked": True,
                "vault_id": loaded.header.vault_id,
                "created": loaded.header.created,
                "signed": loaded.header.manifest is not None,
                "size_bytes": os.path.getsize(args.vault)})


def cmd_store(args) -> None:
    v = _open_vault(args)
    out = v.store(args.text, caller=args.caller, namespace=args.namespace,
                  tags=args.tag or [], importance=args.importance,
                  quarantined=args.quarantined)
    _print(out)


def cmd_search(args) -> None:
    v = _open_vault(args)
    out = v.search(args.query, caller=args.caller, namespace=args.namespace,
                   tags=args.tag or None, top_k=args.top_k)
    if args.json:
        _print(out)
    else:
        for r in out["results"]:
            q = " ⚠QUARANTINED" if r.get("quarantined") else ""
            print(f"[{r['cosine']:.3f}] ({r['namespace']}){q} {r['text']}")
        print(f"-- {out['note']}")
    v.save()


def _ts(s: str | None) -> float | None:
    """Deterministic timestamp parse: unix float, or ISO date/datetime."""
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        import datetime as _dt
        return _dt.datetime.fromisoformat(s).timestamp()


def cmd_link(args) -> None:
    v = _open_vault(args)
    out = v.link(args.subject, args.predicate, args.object, caller=args.caller,
                 namespace=args.namespace, src_id=args.src,
                 valid_from=_ts(getattr(args, "from")), valid_to=_ts(args.to))
    _print(out)
    v.save()


def cmd_relations(args) -> None:
    v = _open_vault(args)
    out = v.relations(caller=args.caller, entity=args.entity,
                      subject=args.subject, predicate=args.predicate,
                      obj=args.object, as_of=_ts(args.as_of),
                      namespace=args.namespace)
    if args.json:
        _print(out)
    else:
        for r in out["relations"]:
            window = ""
            if r["valid_from"] or r["valid_to"]:
                window = f"  [{r['valid_from'] or '…'} → {r['valid_to'] or '…'}]"
            print(f"{r['subject']} -[{r['predicate']}]→ {r['object']}"
                  f"{window}  ({r['id'][:8]})")
        print(f"-- {len(out['relations'])} relations. {out['note']}")
    v.save()


def cmd_unlink(args) -> None:
    v = _open_vault(args)
    _print(v.unlink(args.id, caller=args.caller))


def cmd_get(args) -> None:
    v = _open_vault(args)
    _print(v.get(args.id, caller=args.caller))


def cmd_forget(args) -> None:
    v = _open_vault(args)
    _print(v.forget(args.id, caller=args.caller, shred=args.shred))


def cmd_export(args) -> None:
    v = _open_vault(args)
    data = v.export_jsonl()
    if args.plaintext:
        print("WARNING: exporting PLAINTEXT memories to disk", file=sys.stderr)
        Path(args.out).write_text(data)
        print(f"exported {data.count(chr(10))} records → {args.out}")
    else:
        _die("export writes plaintext; pass --plaintext to confirm you want that")
    v.save()


def cmd_import(args) -> None:
    v = _open_vault(args)
    n = v.import_jsonl(Path(args.file).read_text(), namespace=args.namespace)
    print(f"imported {n} records")


def cmd_rekey(args) -> None:
    v = _open_vault(args)
    pw = getpass.getpass("NEW passphrase: ")
    if pw != getpass.getpass("Repeat NEW passphrase: "):
        _die("passphrases do not match")
    words = v.rekey(pw)
    print("\n=== NEW RECOVERY PHRASE (shown exactly once) ===")
    for i in range(0, 16, 4):
        print("   " + "  ".join(words[i:i + 4]))
    keychain_clear(args.vault)
    print("keychain credential cleared (old key); run `engram unlock --keychain` "
          "to store the new one")


def cmd_audit(args) -> None:
    v = _open_vault(args)
    ok, n, msg = audit.verify(v.db.conn)
    _print({"ok": ok, "entries": n, "message": msg,
            "head": audit.head(v.db.conn)})
    if not ok:
        sys.exit(2)


def cmd_verify(args) -> None:
    loaded = read_vault_file(args.vault)   # structure + format checks
    out = {"vault": args.vault, "format": "ok",
           "vault_id": loaded.header.vault_id,
           "journal_entries": len(loaded.journal_cts)}
    if loaded.header.manifest:
        m = verify_manifest(loaded)
        out["manifest"] = {"ok": True, "creator": m["creator"],
                           "signer_pub": m["signer_pub"][:16] + "…"}
    else:
        out["manifest"] = "vault is not signed (lock --sign to seal)"
    _print(out)


def cmd_selftest(args) -> None:
    v = _open_vault(args)
    out = selftest.run(v)
    _print(out)
    v.save()
    if out["failed"]:
        sys.exit(2)


def cmd_bench(args) -> None:
    if args.longmemeval:
        from . import longmemeval
        _print(longmemeval.run(variant=args.variant, limit=args.limit))
        return
    from . import bench
    v = _open_vault(args)
    _print(bench.run(v, synthetic_n=args.records))


def cmd_reindex(args) -> None:
    if args.re_embed:
        try:
            pw, key = Vault.resolve_credential(args.vault)
        except CryptoError:
            pw = getpass.getpass(f"Passphrase for {args.vault}: ")
            key = None
        v = Vault.unlock(args.vault, passphrase=pw, raw_key=key,
                         check_model=False)
        n = v.reembed(model_name=args.model or DEFAULT_MODEL,
                      caller=args.caller)
        print(f"re-embedded {n} records with {v.header.model['name']} "
              "(fully offline)")
    else:
        v = _open_vault(args)
    precision = "i8" if args.int8 else "f32"
    v.config.settings["index_precision"] = precision
    v.config.save(args.vault)
    v._rebuild_index()
    v.save()
    print(f"reindexed: {v.index.kind}, {len(v.index)} vectors")


def cmd_serve(args) -> None:
    from . import server
    argv = ["--vault", args.vault, "--caller", args.caller]
    if args.assert_offline:
        argv.append("--assert-offline")
    server.main(argv)


def cmd_dash(args) -> None:
    if offline_guard.is_active():
        _die("dash shows a local page over 127.0.0.1, which needs one loopback "
             "socket; the offline guard blocks creating ANY inet socket. Run "
             "dash without --assert-offline (it still makes zero outbound "
             "connections).")
    from . import dash
    v = _open_vault(args)
    dash.run(args.vault, v)


# ------------------------------------------------------------------- packs

def cmd_pack_build(args) -> None:
    src = Path(args.source)
    if src.suffix == ".jsonl":
        records = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    elif src.suffix == ".csv":
        import csv
        with open(src) as f:
            records = [{"text": row["text"],
                        "tags": [t for t in row.get("tags", "").split(";") if t]}
                       for row in csv.DictReader(f)]
    elif src.is_dir():
        records = [{"text": p.read_text().strip(), "tags": [p.stem]}
                   for p in sorted(src.glob("*.md"))]
    else:
        _die("source must be a .jsonl, .csv, or a directory of .md files")
    if not records:
        _die("no records found in source")
    ident_path = Path(args.identity)
    if ident_path.exists():
        identity = json.loads(ident_path.read_text())
    else:
        identity = packs.new_identity(args.creator)
        ident_path.write_text(json.dumps(identity, indent=2))
        print(f"generated new signing identity → {ident_path} (keep it private)")
    emb = Embedder(DEFAULT_MODEL)
    vectors = emb.embed_passages([r["text"] for r in records])
    pw = None
    if args.encrypt:
        pw = getpass.getpass("Pack passphrase: ")
    blob = packs.build_pack(
        name=args.name, version=args.version, description=args.description,
        records=records, vectors=vectors,
        model={"name": DEFAULT_MODEL, "sha256": emb.model_sha256, "dim": emb.dim},
        identity=identity, passphrase=pw)
    out = args.out or f"{args.name}-{args.version}.mpack"
    Path(out).write_bytes(blob)
    print(f"built {out}: {len(records)} records, {len(blob)/1024:.0f} KB, "
          f"signed by {identity['signer']}")


def cmd_pack_install(args) -> None:
    v = _open_vault(args)
    pw = getpass.getpass("Pack passphrase: ") if args.encrypted else None
    out = packs.install_pack(v, Path(args.file).read_bytes(), caller=args.caller,
                             passphrase=pw, allow_reembed=args.re_embed)
    _print(out)


def cmd_pack_remove(args) -> None:
    v = _open_vault(args)
    n = packs.remove_pack(v, args.name, caller=args.caller)
    print(f"removed pack {args.name!r} ({n} records)")


def cmd_pack_list(args) -> None:
    v = _open_vault(args)
    _print(v.pack_list())


def cmd_pack_export(args) -> None:
    """Dump a .mpack's records to editable JSONL (for hand-editing, then
    rebuilding with `engram pack build`)."""
    header, records, _vectors = packs.read_pack(Path(args.file).read_bytes())
    out = Path(args.out)
    with open(out, "w") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"exported {header['name']}@{header['version']}: {len(records)} "
          f"records → {out}")
    print("edit the JSONL, then rebuild + re-sign with:")
    print(f"  engram pack build {out} --name {header['name']} "
          f"--version <bump> --identity <identity.json> --out <file.mpack>")


# --------------------------------------------------------------- integrate

def cmd_integrate(args) -> None:
    """One-command wiring into an agent ecosystem (hermes / claude)."""
    import shutil
    import subprocess as sp
    target = args.target
    vault = args.vault

    if target == "hermes":
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        plug_src = _data_dir() / "hermes-plugin"
        plug_dst = hermes_home / "plugins" / "engram"
        plug_dst.mkdir(parents=True, exist_ok=True)
        for f in ("__init__.py", "plugin.yaml"):
            shutil.copy2(plug_src / f, plug_dst / f)
        print(f"✓ provider plugin installed → {plug_dst}")
        # engram must be importable from Hermes's own venv
        hermes_py = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
        if hermes_py.exists():
            r = sp.run([str(hermes_py), "-c", "import engram"], capture_output=True)
            if r.returncode != 0:
                print("  installing engram-vault into the Hermes venv…")
                sp.run([str(hermes_py), "-m", "pip", "install", "-q",
                        "engram-vault"], check=False)
        if not os.path.exists(vault):
            print(f"! no vault at {vault} - run `engram init` first, then re-run "
                  "this command")
            return
        hermes = shutil.which("hermes")
        if hermes:
            print("  selecting engram in Hermes…")
            sp.run([hermes, "memory", "setup", "engram"], check=False)
        else:
            print("  finish selection with:  hermes memory setup engram")
        print("Done. Verify with:  hermes memory status")

    elif target == "openclaw":
        engram_bin = shutil.which("engram") or "engram"
        entry = {"command": engram_bin,
                 "args": ["--vault", vault, "--caller", "openclaw", "serve"]}
        cfg_path = Path(os.environ.get("OPENCLAW_HOME",
                                       Path.home() / ".openclaw")) / "openclaw.json"
        wrote = False
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text())
                backup = cfg_path.with_suffix(".json.bak-engram")
                backup.write_text(cfg_path.read_text())
                cfg.setdefault("mcpServers", {})["engram"] = entry
                cfg_path.write_text(json.dumps(cfg, indent=2))
                wrote = True
                print(f"✓ registered in {cfg_path} (backup: {backup.name})")
                print("  restart to load:  openclaw gateway restart")
                print("  verify:           openclaw mcp list")
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  could not edit {cfg_path} automatically ({exc});")
        if not wrote:
            print("  add this under \"mcpServers\" in ~/.openclaw/openclaw.json, "
                  "then run `openclaw gateway restart`:")
            print(json.dumps({"engram": entry}, indent=2))
        if not os.path.exists(vault):
            print(f"\n! no vault at {vault} - run `engram init` to create one")

    elif target == "claude":
        engram_bin = shutil.which("engram") or "engram"
        claude = shutil.which("claude")
        if claude:
            print("  registering the engRAM MCP server with Claude Code…")
            r = sp.run([claude, "mcp", "add", "--scope", "user", "engram", "--",
                        engram_bin, "--vault", vault, "--caller", "claude-code",
                        "serve"], capture_output=True, text=True)
            print((r.stdout or r.stderr).strip() or "  registered.")
        else:
            print("  Claude Code CLI not found; register manually with:")
            print(f"    claude mcp add --scope user engram -- {engram_bin} "
                  f"--vault {vault} --caller claude-code serve")
        print("\n  For Claude Desktop, add to claude_desktop_config.json:")
        print(json.dumps({"mcpServers": {"engram": {
            "command": engram_bin,
            "args": ["--vault", vault, "--caller", "claude-desktop", "serve"],
        }}}, indent=2))
        print("\n  Tip: add to your CLAUDE.md so Claude treats this as its memory:")
        print("    Use engram memory_search to recall prior facts before "
              "answering\n    about past work; store durable facts and user "
              "decisions with memory_store.")
        if not os.path.exists(vault):
            print(f"\n! no vault at {vault} - run `engram init` to create one")
    else:
        _die(f"unknown integrate target {target!r} (hermes | claude)")


# ------------------------------------------------------------------- setup

def cmd_setup(args) -> None:
    if args.setup_cmd == "download-model":
        if offline_guard.is_active():
            _die("offline guard is active; refusing the only network operation")
        name = args.model
        if name not in OPTIONAL_MODELS:
            _die(f"unknown model {name!r}; options: {', '.join(OPTIONAL_MODELS)}")
        print("NOTE: this is the ONLY network operation engRAM has. "
              "Everything else is offline forever.")
        import urllib.request
        spec = OPTIONAL_MODELS[name]
        d = user_model_dir() / name
        d.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for fname, url in spec["files"].items():
            print(f"  downloading {fname} …")
            with urllib.request.urlopen(url) as r:
                data = r.read()
            (d / fname).write_bytes(data)
            hashes[fname] = hashlib.sha256(data).hexdigest()
            print(f"    sha256 {hashes[fname]}")
        pins = {"dim": spec["dim"], "files": hashes,
                "prefix_query": spec.get("prefix_query", ""),
                "prefix_passage": spec.get("prefix_passage", "")}
        (d / "HASHES.json").write_text(json.dumps(pins, indent=2))
        print(f"installed model {name} → {d} (hashes pinned)")
    elif args.setup_cmd == "download-longmemeval":
        if offline_guard.is_active():
            _die("offline guard is active; refusing a network operation")
        print("NOTE: like download-model, this is an explicit, user-invoked "
              "network operation. The benchmark run itself is fully offline.")
        from . import longmemeval
        longmemeval.download(args.variant)
    elif args.setup_cmd == "airgap-bundle":
        out = Path(args.out or "engram-airgap.zip")
        root = Path(__file__).resolve().parent
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            pkg_root = root.parent
            for p in sorted(root.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, "engram_pkg/" + str(p.relative_to(pkg_root)))
            for extra in (args.pack or []):
                z.write(extra, "packs/" + Path(extra).name)
            z.writestr("INSTALL.txt",
                       "engRAM air-gap bundle\n"
                       "1. Copy to the target machine (USB).\n"
                       "2. pip install pynacl argon2-cffi onnxruntime tokenizers "
                       "numpy usearch mcp (from a local wheelhouse).\n"
                       "3. Unzip; put engram_pkg/engram on PYTHONPATH or "
                       "site-packages.\n"
                       "4. Run: python -m engram.cli init\n"
                       "The DEFAULT install already contains the model and seed "
                       "pack - this bundle exists for machines with no network "
                       "at all.\n")
        h = hashlib.sha256(out.read_bytes()).hexdigest()
        print(f"wrote {out} ({out.stat().st_size//1024//1024} MB)\nsha256 {h}")
    else:
        print(f"engRAM {__version__} setup\n"
              f"  bundled model: {DEFAULT_MODEL} (offline, no download needed)\n"
              f"  optional models: {', '.join(OPTIONAL_MODELS)}\n"
              f"    → engram setup download-model <name>   (the ONLY network op)\n"
              f"  air-gap bundle: engram setup airgap-bundle --out engram.zip\n"
              f"  model dir: {user_model_dir()}")


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    offline_guard.activate_from_env()
    ap = argparse.ArgumentParser(
        prog="engram",
        description="engRAM - high-security offline vector memory for AI agents")
    ap.add_argument("--vault", default=DEFAULT_VAULT,
                    help=f"vault path (default {DEFAULT_VAULT})")
    ap.add_argument("--caller", default="user")
    ap.add_argument("--assert-offline", action="store_true",
                    help="abort the process if anything attempts network access")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a new vault (+ seed pack)")
    p.add_argument("--passphrase", help="non-interactive (scripting)")
    p.add_argument("--creator", default="user")
    p.add_argument("--keychain", action="store_true",
                   help="store a reboot-surviving Keychain credential (macOS)")
    p.add_argument("--no-session", action="store_true",
                   help="do not stay unlocked after init")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser(
        "unlock",
        help="unlock: stays open until restart/power loss or `engram lock`")
    p.add_argument("--passphrase")
    p.add_argument("--keychain", action="store_true",
                   help="macOS Keychain instead: persists across reboots")
    p.add_argument("--once", action="store_true",
                   help="verify only; store no credential")
    p.set_defaults(fn=cmd_unlock)

    p = sub.add_parser("lock", help="clear stored credential (vault stays sealed)")
    p.add_argument("--sign", action="store_true",
                   help="seal with an Ed25519 signed manifest before locking")
    p.add_argument("--identity", default=str(Path.home() / ".engram" / "identity.json"))
    p.add_argument("--creator", default="vault-owner")
    p.set_defaults(fn=cmd_lock)

    p = sub.add_parser("status", help="vault status")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("store", help="store one memory")
    p.add_argument("text")
    p.add_argument("--namespace")
    p.add_argument("--tag", action="append")
    p.add_argument("--importance", type=float, default=0.5)
    p.add_argument("--quarantined", action="store_true")
    p.set_defaults(fn=cmd_store)

    p = sub.add_parser("search", help="hybrid search")
    p.add_argument("query")
    p.add_argument("--namespace")
    p.add_argument("--tag", action="append")
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("link", help="map a relation: SUBJECT PREDICATE OBJECT")
    p.add_argument("subject")
    p.add_argument("predicate")
    p.add_argument("object")
    p.add_argument("--namespace")
    p.add_argument("--src", help="memory record id this relation came from")
    p.add_argument("--from", help="valid from (ISO date or unix time)")
    p.add_argument("--to", help="valid until (ISO date or unix time)")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("relations", help="query the memory graph")
    p.add_argument("--entity", help="match subject OR object")
    p.add_argument("--subject")
    p.add_argument("--predicate")
    p.add_argument("--object")
    p.add_argument("--as-of", dest="as_of", help="ISO date or unix time")
    p.add_argument("--namespace")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_relations)

    p = sub.add_parser("unlink", help="remove one relation by id")
    p.add_argument("id")
    p.set_defaults(fn=cmd_unlink)

    p = sub.add_parser("get", help="fetch one memory by id")
    p.add_argument("id")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("forget", help="delete a memory (--shred = unrecoverable)")
    p.add_argument("id")
    p.add_argument("--shred", action="store_true")
    p.set_defaults(fn=cmd_forget)

    p = sub.add_parser("export", help="JSONL escape hatch (requires --plaintext)")
    p.add_argument("out")
    p.add_argument("--plaintext", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("import", help="import JSONL records")
    p.add_argument("file")
    p.add_argument("--namespace")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("rekey", help="replace passphrase + recovery phrase")
    p.set_defaults(fn=cmd_rekey)

    pa = sub.add_parser("audit", help="audit log operations")
    pa_sub = pa.add_subparsers(dest="audit_cmd", required=True)
    p = pa_sub.add_parser("verify", help="verify the hash chain")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("verify", help="check vault structure + signed manifest")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("selftest", help="seed-pack health check with latencies")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("bench", help="perf + RAM benchmark; --longmemeval for "
                                     "the retrieval accuracy benchmark")
    p.add_argument("--records", type=int, default=20000)
    p.add_argument("--longmemeval", action="store_true",
                   help="run LongMemEval retrieval (needs the dataset: "
                        "engram setup download-longmemeval)")
    p.add_argument("--variant", default="s", choices=["s", "m", "oracle"])
    p.add_argument("--limit", type=int, help="score only the first N questions")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("reindex", help="rebuild the vector index / migrate models")
    p.add_argument("--int8", action="store_true")
    p.add_argument("--f32", action="store_true")
    p.add_argument("--re-embed", action="store_true",
                   help="re-embed every record with --model (default: bundled)")
    p.add_argument("--model")
    p.set_defaults(fn=cmd_reindex)

    p = sub.add_parser("serve", help="run the MCP stdio server")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("dash", help="open the vault dashboard in your browser")
    p.set_defaults(fn=cmd_dash)

    pp = sub.add_parser("pack", help="memory packs")
    pp_sub = pp.add_subparsers(dest="pack_cmd", required=True)
    p = pp_sub.add_parser("build")
    p.add_argument("source", help=".jsonl / .csv / directory of .md files")
    p.add_argument("--name", required=True)
    p.add_argument("--version", default="1.0.0")
    p.add_argument("--description", default="")
    p.add_argument("--creator", default="pack-author")
    p.add_argument("--identity", default=str(Path.home() / ".engram" / "identity.json"))
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_pack_build)
    p = pp_sub.add_parser("install")
    p.add_argument("file")
    p.add_argument("--re-embed", action="store_true")
    p.add_argument("--encrypted", action="store_true")
    p.set_defaults(fn=cmd_pack_install)
    p = pp_sub.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(fn=cmd_pack_remove)
    p = pp_sub.add_parser("list")
    p.set_defaults(fn=cmd_pack_list)
    p = pp_sub.add_parser("export", help="dump a .mpack to editable JSONL")
    p.add_argument("file")
    p.add_argument("out")
    p.set_defaults(fn=cmd_pack_export)

    p = sub.add_parser("integrate",
                       help="one-command wiring into claude, hermes, or openclaw")
    p.add_argument("target", choices=["claude", "hermes", "openclaw"])
    p.set_defaults(fn=cmd_integrate)

    ps = sub.add_parser("setup", help="models + air-gap bundles")
    ps_sub = ps.add_subparsers(dest="setup_cmd")
    p = ps_sub.add_parser("download-model", help="THE only network operation")
    p.add_argument("model")
    p.set_defaults(fn=cmd_setup)
    p = ps_sub.add_parser("download-longmemeval",
                          help="fetch the LongMemEval benchmark dataset "
                               "(explicit network operation)")
    p.add_argument("--variant", default="s", choices=["s", "m", "oracle"])
    p.set_defaults(fn=cmd_setup)
    p = ps_sub.add_parser("airgap-bundle")
    p.add_argument("--out")
    p.add_argument("--pack", action="append")
    p.set_defaults(fn=cmd_setup)
    ps.set_defaults(fn=cmd_setup, setup_cmd=None)

    args = ap.parse_args(argv)
    if args.assert_offline:
        offline_guard.activate()
    try:
        args.fn(args)
    except CryptoError as exc:
        _die(str(exc))


if __name__ == "__main__":
    main()
