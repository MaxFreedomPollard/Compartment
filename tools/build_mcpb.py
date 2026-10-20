"""Build dist/compartment-<version>.mcpb, the Claude Desktop extension.

An MCP Bundle is a zip with manifest.json at its root (the format lives in
github.com/modelcontextprotocol/mcpb, formerly anthropics/dxt). This one is
server type "uv", manifest_version 0.4: it carries a pyproject.toml that
pins the release, a three-line entry point and the icon, and Claude Desktop
has uv install compartment and its compiled dependencies for the user's own
platform when the extension is installed. Bundling the dependencies instead
("python" type) cannot work for Compartment: onnxruntime, PyNaCl,
argon2-cffi, numpy and tokenizers are compiled, so a bundle built on one
platform is dead on the others, and the MCPB README says as much.

    python tools/build_mcpb.py            # what the release runs; needs uv
    python tools/build_mcpb.py --no-lock  # no uv.lock (development, tests)

With uv on the PATH the bundle also carries a uv.lock, resolved here, with
hashes, for every platform: Claude Desktop then installs exactly what the
release was built against instead of resolving again on each user's
machine, and a launch whose environment is already in place has nothing to
look up. Resolving is the one step that uses the network, and it happens at
build time, on the release runner. The built bundle and the server in it
make no connection.

Standard library only, and deterministic: the same inputs give the same
bytes, so a rebuilt bundle can be compared with the one attached to a
release.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "packaging" / "mcpb"

#: Archive path -> source file. The manifest's entry point and icon must be
#: in here, and nothing that is not declared ships.
CONTENTS = {
    "manifest.json": SRC / "manifest.json",
    "pyproject.toml": SRC / "pyproject.toml",
    ".python-version": SRC / ".python-version",
    "server/main.py": SRC / "server" / "main.py",
    "icon.png": ROOT / "docs" / "images" / "logo-400.png",
    "LICENSE": ROOT / "LICENSE",
}

#: 1980-01-01, the earliest time a zip can record: every entry gets it, so
#: the archive does not change with the checkout's file times.
_EPOCH = (1980, 1, 1, 0, 0, 0)


def package_version() -> str:
    text = (ROOT / "src" / "compartment" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("cannot find __version__ in src/compartment/__init__.py")
    return m.group(1)


def semver(v: str) -> str:
    parts = v.split(".")
    return ".".join(parts + ["0"] * (3 - len(parts)))


def check(version: str) -> dict:
    """Refuse to build a bundle that would install the wrong release or
    that Claude Desktop would reject. Returns the manifest."""
    manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    for field in ("manifest_version", "name", "version", "description",
                  "author", "server"):
        if field not in manifest:
            problems.append(f"manifest.json has no {field}")
    if manifest.get("version") != semver(version):
        problems.append(f"manifest.json version {manifest.get('version')!r} is "
                        f"not the package's {semver(version)!r}")
    pin = re.search(r'"compartment==([^"]+)"',
                    (SRC / "pyproject.toml").read_text(encoding="utf-8"))
    if not pin or pin.group(1) != version:
        problems.append(f"pyproject.toml must pin compartment=={version}")
    server = manifest.get("server", {})
    if server.get("type") != "uv":
        problems.append("server.type must be uv: the dependencies are compiled")
    for declared in (server.get("entry_point"), manifest.get("icon")):
        if declared and declared not in CONTENTS:
            problems.append(f"manifest.json names {declared}, which the bundle "
                            "does not contain")
    for name, source in CONTENTS.items():
        if not source.is_file():
            problems.append(f"{name}: {source} is missing")
    if problems:
        raise SystemExit("cannot build the bundle:\n  " + "\n  ".join(problems))
    return manifest


def lock(staging: pathlib.Path) -> pathlib.Path:
    """Resolve uv.lock for the pinned release. Network: PyPI, build time only."""
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is not on PATH, so the bundle cannot carry a lock "
                         "file. Install it (`pip install uv`) and run again, or "
                         "pass --no-lock for a development build.")
    for name in ("pyproject.toml", ".python-version"):
        shutil.copy2(SRC / name, staging / name)
    if subprocess.run([uv, "lock", "--directory", str(staging)]).returncode:
        # Almost always the release is not on PyPI yet: the lock pins the
        # version this bundle installs, so it can only be resolved after
        # publish.yml has uploaded it. uv has printed its own reason above.
        raise SystemExit(f"uv could not lock compartment=={package_version()}. "
                         "If that version is not on PyPI yet, build the bundle "
                         "after the PyPI publish; --no-lock skips the lock.")
    return staging / "uv.lock"


def build(out_dir: pathlib.Path, with_lock: bool = True) -> pathlib.Path:
    version = package_version()
    check(version)
    files = dict(CONTENTS)
    with tempfile.TemporaryDirectory() as tmp:
        if with_lock:
            files["uv.lock"] = lock(pathlib.Path(tmp))
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"compartment-{version}.mcpb"
        partial = out.with_suffix(".mcpb.partial")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(files):
                info = zipfile.ZipInfo(name, date_time=_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, files[name].read_bytes())
        partial.replace(out)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist"),
                    help="directory to write the bundle into (default dist/)")
    ap.add_argument("--no-lock", action="store_true",
                    help="leave out uv.lock (no uv, no network); not for release")
    args = ap.parse_args(argv)
    out = build(pathlib.Path(args.out), with_lock=not args.no_lock)
    print(f"built {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())
