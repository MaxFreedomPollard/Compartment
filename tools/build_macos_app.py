"""Build engRAM.app - a self-contained macOS menu bar app - and its installer.

    python tools/build_macos_app.py            # build build/engRAM.app
    python tools/build_macos_app.py --dmg      # …and a drag-to-install .dmg
    python tools/build_macos_app.py --pkg      # …and a .pkg with an optional
                                               #   "start at login" component

Why the app has to embed its own Python
---------------------------------------
A status bar item belongs to whatever process created it, and macOS decides
what that process *is* from where its executable lives. Launch a script that
`exec`s a Python living outside the bundle and `NSBundle.mainBundle()` comes
back as the interpreter's directory: no bundle identifier, no name, no icon.
Such a process is treated as anonymous - it sorts last in the menu bar and is
the first thing hidden behind the notch, which looks exactly like "the app
does not work". It is also why it would show up nameless in System Settings.

So the interpreter is copied *inside* the bundle. One wrinkle: CPython infers
a venv's root by assuming the executable sits in `<root>/bin`, so with the
binary at `Contents/MacOS/python` the root is `Contents` - which is where
`pyvenv.cfg` and `lib/pythonX.Y/site-packages` must go, not `Contents/MacOS`.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
APP_NAME = "engRAM"
BUNDLE_ID = "io.github.maxfreedompollard.engram"
# Shown under the app's name in System Settings > General > Login Items.
DESCRIPTION = ("engRAM keeps your AI agents' memory encrypted on this Mac. "
               "The menu bar item shows what it has remembered and lets you "
               "change its settings.")


def _version() -> str:
    ns: dict = {}
    exec((ROOT / "src" / "engram" / "__init__.py").read_text(
        encoding="utf-8").split("from .")[0], ns)
    return ns.get("__version__", "0.0.0")


def _run(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], check=True, **kw)


def build_icon(dest: pathlib.Path) -> pathlib.Path:
    sys.path.insert(0, str(ROOT / "tools"))
    from make_icon import build as build_icns
    return build_icns(dest)


def find_standalone_python() -> str:
    """A NON-framework interpreter to embed.

    A framework build (python.org, Homebrew's framework variant) is a stub
    that hands off to `Python.framework/Resources/Python.app`, so copying it
    into the bundle produces an app that reports itself as `org.python.python`
    - the very identity problem the embedding is meant to solve. Standalone
    builds (uv's python-build-standalone) have no such indirection.
    """
    def ok(exe: str) -> bool:
        try:
            out = subprocess.run(
                [exe, "-c", "import sys,sysconfig;"
                 "print(sysconfig.get_config_var('PYTHONFRAMEWORK') or '')"],
                capture_output=True, text=True, timeout=30)
            return out.returncode == 0 and not out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False

    if ok(sys.executable):
        return sys.executable
    uv_root = pathlib.Path.home() / ".local" / "share" / "uv" / "python"
    for d in sorted(uv_root.glob("cpython-3.*-macos-*"), reverse=True):
        for exe in sorted(d.glob("bin/python3.*"), reverse=True):
            if exe.is_file() and ok(str(exe)):
                return str(exe)
    if shutil.which("uv"):
        print("fetching a standalone Python via uv…")
        _run("uv", "python", "install", "3.12")
        for d in sorted(uv_root.glob("cpython-3.*-macos-*"), reverse=True):
            for exe in sorted(d.glob("bin/python3.*"), reverse=True):
                if exe.is_file() and ok(str(exe)):
                    return str(exe)
    raise SystemExit(
        "error: need a non-framework Python to embed.\n"
        "  install one with:  uv python install 3.12\n"
        "  or pass --python /path/to/standalone/python")


def _strip(contents: pathlib.Path) -> None:
    """Drop what a shipped app has no use for.

    PyObjC vendors its own test suite and 142 .dSYM debug bundles - 16 MB of
    dead weight that pkgbuild also insists on cataloguing as nested bundles,
    which makes the installer slower to build and larger to download.
    """
    freed = 0
    lib = contents / "lib"
    doomed: list[pathlib.Path] = []
    for pattern in ("**/PyObjCTest", "**/__pycache__", "**/*.dSYM",
                    "**/tests", "**/test"):
        doomed.extend(p for p in lib.glob(pattern) if p.is_dir())
    for p in doomed:
        if not p.exists():
            continue
        freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        shutil.rmtree(p, ignore_errors=True)
    # pip and setuptools are build-time only; the app never installs anything
    for name in ("pip", "setuptools", "pkg_resources", "_distutils_hack"):
        for p in lib.glob(f"**/{name}"):
            if p.is_dir():
                freed += sum(f.stat().st_size for f in p.rglob("*")
                             if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
    print(f"stripped {freed / 1024 / 1024:.0f} MB of test and build files")


def build_app(spec: str | None = None, out: pathlib.Path | None = None,
              python: str | None = None) -> pathlib.Path:
    """spec: what to pip-install (default: this checkout, so the build always
    matches the source it came from)."""
    version = _version()
    base_python = python or find_standalone_python()
    print(f"embedding {base_python}")
    app = (out or BUILD / f"{APP_NAME}.app")
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # 1. a venv rooted at Contents (see the module docstring for why)
    print("creating the embedded environment…")
    _run(base_python, "-m", "venv", "--copies", contents)
    pip = contents / "bin" / "pip"
    _run(pip, "install", "--quiet", "--upgrade", "pip")
    _run(pip, "install", "--quiet", spec or str(ROOT))
    _run(pip, "install", "--quiet", "pyobjc-framework-Cocoa>=10.0")

    _strip(contents)

    # 2. the interpreter must live INSIDE the bundle for it to have identity
    py = contents / "bin" / f"python{sysconfig.get_python_version()}"
    if not py.exists():
        py = contents / "bin" / "python3"
    shutil.copy2(py, macos / "python")
    (macos / "python").chmod(0o755)

    # 3. the executable named by Info.plist
    launcher = macos / APP_NAME
    launcher.write_text(
        "#!/bin/sh\n"
        '# Keep the running image inside the bundle - see build_macos_app.py\n'
        'here="$(cd "$(dirname "$0")" && pwd)"\n'
        'exec "$here/python" -m engram.cli menubar "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)

    build_icon(resources / f"{APP_NAME}.icns")

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": f"{APP_NAME}.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSUIElement": True,               # menu bar only, no dock icon
        "LSMinimumSystemVersion": "13.0",
        "NSHumanReadableCopyright": "MIT licensed. https://github.com/"
                                    "MaxFreedomPollard/engRAM",
        "NSHumanReadableDescription": DESCRIPTION,
        "LSApplicationCategoryType": "public.app-category.productivity",
    }
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)

    # 4. ad-hoc signature: without it macOS may refuse to keep the login item,
    #    and the identifier is what System Settings groups the item under.
    _run("codesign", "--force", "--deep", "--sign", "-",
         "--identifier", BUNDLE_ID, app)
    print(f"built {app}")
    return app


def _login_plist(app: pathlib.Path) -> dict:
    return {
        "Label": BUNDLE_ID,
        "ProgramArguments": [str(pathlib.Path("/Applications") / app.name
                                 / "Contents" / "MacOS" / APP_NAME)],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }


def build_pkg(app: pathlib.Path) -> pathlib.Path:
    """A .pkg whose second, optional component starts it at login."""
    version = _version()
    staging = BUILD / "pkgroot"
    login_root = BUILD / "pkgroot-login"
    for d in (staging, login_root):
        if d.exists():
            shutil.rmtree(d)
    (staging / "Applications").mkdir(parents=True)
    shutil.copytree(app, staging / "Applications" / app.name, symlinks=True)
    login_root.mkdir(parents=True)          # payload-free: scripts only
    scripts = BUILD / "login-scripts"
    if scripts.exists():
        shutil.rmtree(scripts)
    scripts.mkdir(parents=True)
    post = scripts / "postinstall"
    post.write_text(
        "#!/bin/sh\n"
        "# Register with SMAppService so System Settings > Login Items lists\n"
        "# engRAM by name, with its icon, instead of a nameless legacy agent.\n"
        f'APP="/Applications/{app.name}"\n'
        'if [ -x "$APP/Contents/MacOS/engRAM" ]; then\n'
        '  su "$USER" -c "\"$APP/Contents/MacOS/engRAM\" --login on" || true\n'
        '  su "$USER" -c "open -a \"$APP\"" || true\n'
        "fi\n"
        "exit 0\n", encoding="utf-8")
    post.chmod(0o755)

    core = BUILD / "core.pkg"
    login = BUILD / "login.pkg"
    _run("pkgbuild", "--root", staging, "--identifier", BUNDLE_ID,
         "--version", version, "--install-location", "/", core)
    _run("pkgbuild", "--root", login_root, "--identifier", BUNDLE_ID + ".login",
         "--version", version, "--install-location", "/",
         "--scripts", BUILD / "login-scripts", "--nopayload", login)

    dist = BUILD / "distribution.xml"
    dist.write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>engRAM</title>
  <options customize="always" require-scripts="false" hostArchitectures="arm64,x86_64"/>
  <volume-check><allowed-os-versions><os-version min="13.0"/></allowed-os-versions></volume-check>
  <choices-outline>
    <line choice="core"/>
    <line choice="login"/>
  </choices-outline>
  <choice id="core" title="engRAM" description="The engRAM app, its encrypted memory vault, and the `engram` command line tool."
          start_selected="true" start_enabled="false">
    <pkg-ref id="{BUNDLE_ID}"/>
  </choice>
  <choice id="login" title="Menu bar utility"
          description="Keep engRAM in the menu bar and start it automatically when you log in. You can turn this off later in System Settings &gt; General &gt; Login Items."
          start_selected="true">
    <pkg-ref id="{BUNDLE_ID}.login"/>
  </choice>
  <pkg-ref id="{BUNDLE_ID}" version="{version}">core.pkg</pkg-ref>
  <pkg-ref id="{BUNDLE_ID}.login" version="{version}">login.pkg</pkg-ref>
</installer-gui-script>
""", encoding="utf-8")

    out = BUILD / f"{APP_NAME}-{version}.pkg"
    _run("productbuild", "--distribution", dist, "--package-path", BUILD, out)
    print(f"built {out}")
    return out


def build_dmg(app: pathlib.Path) -> pathlib.Path:
    version = _version()
    stage = BUILD / "dmg"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    os.symlink("/Applications", stage / "Applications")
    out = BUILD / f"{APP_NAME}-{version}.dmg"
    out.unlink(missing_ok=True)
    _run("hdiutil", "create", "-volname", f"{APP_NAME} {version}",
         "-srcfolder", stage, "-ov", "-format", "UDZO", out)
    print(f"built {out}")
    return out


def main() -> int:
    if sys.platform != "darwin":
        print("error: macOS only", file=sys.stderr)
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="what to pip install (default: this repo)")
    ap.add_argument("--python", help="standalone interpreter to embed")
    ap.add_argument("--dmg", action="store_true")
    ap.add_argument("--pkg", action="store_true")
    args = ap.parse_args()
    BUILD.mkdir(exist_ok=True)
    app = build_app(args.spec, python=args.python)
    if args.dmg:
        build_dmg(app)
    if args.pkg:
        build_pkg(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
