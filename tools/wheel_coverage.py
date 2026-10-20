"""Fail when a plain install would have to compile something on a supported platform.

    python tools/wheel_coverage.py            # needs `uv` on PATH

pip installs the newest release of every dependency, direct or not, whether
or not that release has a wheel for the machine, and then tries to build it
from source. A user without a compiler (or a Rust toolchain, for
cryptography) gets a wall of build errors from a package they never asked
for. That is how `pip install compartment` broke on Windows on ARM and on
Intel Macs in 2026: cryptography and argon2-cffi-bindings stopped shipping
wheels there and nothing here noticed.

For every platform and Python Compartment supports, this resolves the
dependencies twice with uv - as pip would, and with wheels only - and fails
if the two disagree, naming the package. pyproject.toml's platform caps are
the fix it points at.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: (uv --python-platform, Pythons, macOS deployment target or None). Kept in
#: step with README's "Where it runs": onnxruntime decides most of it.
SUPPORTED = [
    ("x86_64-pc-windows-msvc", ["3.11", "3.12", "3.13", "3.14"], None),
    # 3.11 on Windows on ARM has no pyyaml wheel (via tokenizers ->
    # huggingface-hub), so ARM64 Windows starts at 3.12.
    ("aarch64-pc-windows-msvc", ["3.12", "3.13", "3.14"], None),
    ("x86_64-manylinux_2_28", ["3.11", "3.12", "3.13", "3.14"], None),
    ("aarch64-manylinux_2_28", ["3.11", "3.12", "3.13", "3.14"], None),
    # onnxruntime's arm64 wheels need macOS 14 from 1.24 on, and 1.24 is the
    # first with 3.14 wheels.
    ("aarch64-apple-darwin", ["3.11", "3.12", "3.13", "3.14"], "14.0"),
    # Intel Macs stop at onnxruntime 1.23.2, which stops at Python 3.13.
    ("x86_64-apple-darwin", ["3.11", "3.12", "3.13"], "13.0"),
]


def _compile(platform: str, python: str, target: str | None,
             binary_only: bool, extra: str | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    if target:
        env["MACOSX_DEPLOYMENT_TARGET"] = target
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "req.txt"
        cmd = ["uv", "pip", "compile", str(ROOT / "pyproject.toml"),
               *(["--extra", extra] if extra else []),
               "--python-platform", platform,
               "--python-version", python, "--no-header", "--no-annotate",
               "-q", "-o", str(out)]
        if binary_only:
            cmd += ["--only-binary", ":all:"]
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        text = out.read_text() if out.exists() else ""
        return p.returncode, text if p.returncode == 0 else p.stderr


def _check(platform: str, py: str, target: str | None,
           extra: str | None) -> str | None:
    """None when a plain install needs no compiler; otherwise why."""
    rc_any, any_ = _compile(platform, py, target, False, extra)
    rc_bin, bin_ = _compile(platform, py, target, True, extra)
    if rc_any != 0:
        return f"does not resolve at all\n{any_}"
    if rc_bin != 0:
        return f"no wheel-only solution exists\n{bin_}"
    if any_ != bin_:
        a, b = set(any_.splitlines()), set(bin_.splitlines())
        return "pip would build from source:\n" + "\n".join(
            [f"     {ln}   (newest; no wheel here)" for ln in sorted(a - b)]
            + [f"     {ln}   (newest with a wheel)" for ln in sorted(b - a)])
    return None


def main() -> int:
    bad = 0
    for platform, pythons, target in SUPPORTED:
        for py in pythons:
            label = f"{platform} / Python {py}"
            why = _check(platform, py, target, None)
            if why:
                print(f"FAIL {label}: {why}")
                bad += 1
                continue
            # The optional [hnsw] extra is reported, not enforced: it is an
            # opt-in for vaults past 20k records, and its own dependency
            # (numkong) has shipped partial wheel sets that fix themselves
            # within days.
            why = _check(platform, py, target, "hnsw")
            print(f"ok   {label}" + (f"   (note: [hnsw] {why})" if why else ""))
    if bad:
        print(f"\n{bad} platform(s) would compile a dependency on install. "
              "Cap the package for that platform in pyproject.toml, with the "
              "reason, or drop the platform from SUPPORTED and the README.")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
