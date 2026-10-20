"""Build Compartment.app - a self-contained macOS menu bar app - and its installer.

    python tools/build_macos_app.py            # build build/Compartment.app
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

Why a venv is the wrong way to do that
--------------------------------------
This script used to build the bundle as a venv rooted at `Contents/`, and it
was wrong twice over.

A venv does not contain a standard library. It contains a `pyvenv.cfg` whose
`home` key points at the interpreter it was made from - here, a uv-managed
Python under the *build machine's* home directory. The resulting app ran
perfectly for whoever built it and could not start on any other Mac, because
`/Users/<someone-else>/.local/share/uv/...` does not exist there. That is not
a packaging nicety; the app was simply broken for everyone but its author.

`pyvenv.cfg` also lands as a stray file in the bundle root, and codesign's
default rules treat any loose file there as *nested code*. A non-Mach-O file
cannot carry an embedded signature, so its signature goes into
`com.apple.cs.*` extended attributes instead - which `hdiutil`, zip, and
plain `cp` all silently drop. The app then reads as "code object is not
signed at all", Gatekeeper calls that `no usable signature`, and a quarantined
copy is SIGKILLed the instant it execs: no dialog, no crash log, no dock
bounce. Clicking the app does nothing whatsoever.

So: the interpreter's whole runtime is copied into `Contents/Resources/`,
which codesign seals as ordinary resources with no extended attributes, and
the launcher points `PYTHONHOME` at it. Nothing sits in the bundle root, and
nothing outside the bundle is needed at runtime. `_verify_self_contained`
enforces both at build time.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
_EMBEDDED_VER = [""]              # X.Y of the interpreter actually embedded
BUILD = ROOT / "build"
APP_NAME = "Compartment"
BUNDLE_ID = "io.github.maxfreedompollard.compartment"
# The oldest macOS the app is built for, and the default of --min-macos. It
# is LSMinimumSystemVersion, the launcher's -mmacosx-version-min, the .pkg's
# allowed-os-versions, and - the part that is easy to miss - the platform
# every wheel in the bundle is chosen for (_install_packages). 13 is also the
# floor: SMAppService, which registers the login item, is macOS 13 and up.
MIN_MACOS = "13.0"
# Shown under the app's name in System Settings > General > Login Items.
DESCRIPTION = ("Compartment keeps your AI agents' memory encrypted on this Mac. "
               "The menu bar item shows what it has remembered and lets you "
               "change its settings.")


def _version() -> str:
    ns: dict = {}
    exec((ROOT / "src" / "compartment" / "__init__.py").read_text(
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
        """Non-framework, not a system interpreter, and carrying its own
        standard library. The whole prefix is copied into the bundle, so a
        system Python would mean copying /usr - and a prefix with no stdlib
        of its own is what produced an app that only ran on the build
        machine."""
        try:
            out = subprocess.run(
                [exe, "-c", "import sys,sysconfig;"
                 "print(sysconfig.get_config_var('PYTHONFRAMEWORK') or '');"
                 "print(sys.prefix);"
                 "print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return False
            lines = (out.stdout.splitlines() + ["", "", ""])[:3]
            framework, prefix, ver = lines
            if framework.strip() or not prefix.strip():
                return False
            p = pathlib.Path(prefix)
            if str(p).startswith(("/usr", "/System", "/Library")):
                return False
            return (p / "lib" / f"python{ver}" / "os.py").is_file()
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


def _python_version(exe: str) -> str:
    """The X.Y of the interpreter being embedded - which is not necessarily
    the X.Y of the interpreter running this script."""
    out = subprocess.run(
        [exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _python_arch(exe: str) -> str:
    """The CPU the embedded interpreter is built for: arm64 or x86_64.

    Asked of that interpreter, not of this one or of the machine. It decides
    which wheels go into the bundle, which architecture the launcher is
    compiled for, and which Macs the installer will agree to run on, and all
    three have to be the interpreter's or the app cannot start."""
    out = subprocess.run(
        [exe, "-c", "import platform;print(platform.machine())"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _version_tuple(text: str) -> tuple[int, int]:
    parts = (str(text).split(".") + ["0"])[:2]
    return int(parts[0]), int(parts[1])


def _install_packages(rt_python: pathlib.Path, spec: str, arch: str,
                      min_macos: str) -> None:
    """Install Compartment and its dependencies into the embedded runtime,
    each as the wheel built for `min_macos` on `arch`.

    pip on its own picks the wheel for the macOS it is running on, and the
    build machine is the newest macOS there is. From onnxruntime 1.24 on,
    the macOS wheels are arm64 only and tagged macosx_14_0, and numpy ships
    a macosx_14_0 build beside its macosx_11_0 one; pip prefers the newer
    tag whenever the machine allows it. So an app built on a current runner
    carried libraries that need macOS 14 inside a bundle declaring 13.0:
    Installer accepted it on macOS 13, and the app could not load its own
    embedding model there.

    So the wheels are chosen for the declared minimum instead. `pip
    download --platform` resolves against that platform (and every older
    one, so universal2 and macosx_11_0 wheels qualify), binary only, into a
    wheelhouse; the install then takes only from the wheelhouse. On arm64
    with a minimum of 13.0 that resolves onnxruntime 1.23.2, the last
    release with a macosx_13_0 build, and on Intel the last with any macOS
    x86_64 build at all. _verify_deployment_target checks the result.
    """
    major, minor = _version_tuple(min_macos)
    platform_tag = f"macosx_{major}_{minor}_{arch}"
    with tempfile.TemporaryDirectory(prefix="compartment-wheels-") as td:
        house = pathlib.Path(td)
        # Compartment itself, as a wheel, so the binary-only resolve below
        # can take it: it is pure Python, built from this checkout (or
        # fetched, for a --spec naming a release).
        _run(rt_python, "-m", "pip", "wheel", "--quiet", "--no-deps",
             "--wheel-dir", house, spec)
        wheels = sorted(house.glob("compartment-*.whl"))
        if not wheels:
            raise SystemExit(f"error: building a wheel from {spec} produced "
                             "no compartment wheel")
        wanted = [str(wheels[-1]), "pyobjc-framework-Cocoa>=10.0"]
        got = subprocess.run(
            [str(rt_python), "-m", "pip", "download", "--quiet",
             "--only-binary=:all:", "--platform", platform_tag,
             "--dest", str(house), *wanted],
            capture_output=True, text=True)
        if got.returncode != 0:
            raise SystemExit(
                f"error: no complete set of wheels for Python "
                f"{_EMBEDDED_VER[0]} on macOS {min_macos} ({arch})\n"
                f"{(got.stderr or got.stdout).strip()[-1500:]}\n"
                "  onnxruntime 1.23.2 is the last release with macOS 13 and "
                "Intel wheels, and it\n  goes up to Python 3.13: embed 3.12 "
                "or 3.13 (uv python install 3.12), or, on\n  Apple silicon "
                "only, build for macOS 14 with --min-macos 14.0")
        print(f"wheels chosen for {platform_tag}: "
              + ", ".join(sorted(p.name.split("-")[0] + " "
                                 + p.name.split("-")[1]
                                 for p in house.glob("*.whl"))))
        _run(rt_python, "-m", "pip", "install", "--quiet", "--no-index",
             "--find-links", house, *wanted)


# Mach-O, read directly: what the loader reads, with no tool to install and
# a check that a test can feed bytes to on any OS.
_FAT_MAGICS = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")
_THIN_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")   # 64, 32 bit LE
_CPU_NAMES = {0x01000007: "x86_64", 0x0100000C: "arm64", 7: "i386",
              12: "arm"}
_LC_VERSION_MIN_MACOSX = 0x24
_LC_BUILD_VERSION = 0x32
_PLATFORM_MACOS = 1


def _macho_thin(f, start: int) -> tuple[str, tuple[int, int] | None] | None:
    """(architecture, minimum macOS) of the Mach-O image at `start`."""
    f.seek(start)
    head = f.read(28)
    if len(head) < 28 or head[:4] not in _THIN_MAGICS:
        return None
    cputype, _sub, _ftype, ncmds, sizeofcmds, _flags = struct.unpack(
        "<IiIIII", head[4:28])
    if head[:4] == _THIN_MAGICS[0]:
        f.read(4)                               # mach_header_64.reserved
    cmds = f.read(sizeofcmds)
    minos, pos = None, 0
    for _ in range(ncmds):
        if pos + 16 > len(cmds):
            break
        cmd, size, a, b = struct.unpack_from("<IIII", cmds, pos)
        if cmd == _LC_BUILD_VERSION and a == _PLATFORM_MACOS:
            minos = (b >> 16, (b >> 8) & 0xFF)
        elif cmd == _LC_VERSION_MIN_MACOSX:
            minos = (a >> 16, (a >> 8) & 0xFF)
        if size < 8:
            break
        pos += size
    return _CPU_NAMES.get(cputype, hex(cputype)), minos


def _macho_slices(path: pathlib.Path) -> dict[str, tuple[int, int] | None]:
    """{architecture: minimum macOS} for each slice of a Mach-O file, or {}
    for anything that is not one. The minimum is None for an image that
    records none."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic in _THIN_MAGICS:
                one = _macho_thin(f, 0)
                return dict([one]) if one else {}
            if magic not in _FAT_MAGICS:
                return {}
            wide = magic == _FAT_MAGICS[1]
            (count,) = struct.unpack(">I", f.read(4))
            # A Java class file starts with the same four bytes, followed by
            # its version, 45 and up. No universal binary has that many
            # slices.
            if not 0 < count <= 16:
                return {}
            entry = ">IIQQII" if wide else ">IIIII"
            offsets = [struct.unpack(entry, f.read(struct.calcsize(entry)))[2]
                       for _ in range(count)]
            out = {}
            for off in offsets:
                one = _macho_thin(f, off)
                if one is None:
                    return {}
                out[one[0]] = one[1]
            return out
    except (OSError, struct.error):
        return {}


def _verify_deployment_target(app: pathlib.Path, arch: str,
                              min_macos: str) -> None:
    """Every binary in the bundle runs on `arch` under `min_macos`.

    The check that would have caught the macOS 14 onnxruntime in a bundle
    declaring 13.0, and an arm64-only library in an Intel build. A Mach-O
    built for a newer macOS than the bundle declares fails to load on the
    older one with nothing on screen - the menu bar app has no window to
    report it in - so this is fatal, as the signature check is."""
    floor = _version_tuple(min_macos)
    too_new, wrong_arch, count = [], [], 0
    for p in sorted(app.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        slices = _macho_slices(p)
        if not slices:
            continue
        count += 1
        rel = p.relative_to(app)
        if arch not in slices:
            wrong_arch.append(f"{rel} ({', '.join(sorted(slices))})")
        elif slices[arch] is not None and slices[arch] > floor:
            need = slices[arch]
            too_new.append(f"{rel} needs macOS {need[0]}.{need[1]}")
    problems = []
    if wrong_arch:
        problems.append(f"built without {arch}, so it cannot load in this "
                        "app:\n  " + "\n  ".join(wrong_arch[:20]))
    if too_new:
        problems.append(f"built for a newer macOS than the {min_macos} the "
                        "app declares:\n  " + "\n  ".join(too_new[:20]))
    if problems:
        raise SystemExit("error: " + "\nerror: ".join(problems))
    print(f"{count} binaries, every one {arch} and none needing more than "
          f"macOS {min_macos}")


def _strip(lib: pathlib.Path) -> None:
    """Drop what a shipped app has no use for.

    PyObjC vendors its own test suite and 142 .dSYM debug bundles - 16 MB of
    dead weight that pkgbuild also insists on cataloguing as nested bundles,
    which makes the installer slower to build and larger to download.
    """
    freed = 0
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


#: What a console script in the bundle starts with: the interpreter beside
#: the script, wherever the bundle is, the form uv gives its own Pythons'
#: scripts. Valid sh and valid Python at once: sh runs the exec line, and
#: Python reads lines two and three as one string and moves on. -B because
#: the bundle is signed, and bytecode written into it breaks the seal.
#: realpath is in macOS 13 and up, which is MIN_MACOS's floor.
_RELOCATABLE_SHEBANG = (
    "#!/bin/sh\n"
    "'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")/{python}\" -B "
    "\"$0\" \"$@\"\n"
    "' '''\n")


def _relocate_scripts(runtime: pathlib.Path) -> list[str]:
    """Rewrite the console scripts pip put in runtime/bin so they run from
    wherever the bundle ends up.

    pip writes the absolute path of the interpreter that installed them into
    each script's first line, and that interpreter is the copy inside the
    BUILD directory. So `compartment` inside the app, which is what the
    panel's Connect buttons write into every agent's config (clients.
    executable picks the console script beside the running interpreter) and
    what the capture hook runs, named a Python on the build machine: on
    anybody else's Mac the agent's memory server failed with "bad
    interpreter" on its first start. The long-path form pip uses for a
    folder with a space in it names the same absolute path, inside an exec
    line, and is rewritten the same way."""
    bin_dir = runtime / "bin"
    here = {str(runtime), str(runtime.resolve())}
    done = []
    for script in sorted(bin_dir.iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        try:
            lines = script.read_text(encoding="utf-8").split("\n")
        except (UnicodeDecodeError, OSError):
            continue                                  # a binary
        first = lines[0]
        if (first.startswith("#!") and len(lines) > 2
                and first.strip() == "#!/bin/sh"
                and lines[1].startswith("'''exec' ")
                and lines[2] == "' '''"):
            named, rest = lines[1], lines[3:]
        elif first.startswith("#!"):
            named, rest = first, lines[1:]
        else:
            continue
        if not any(h in named for h in here):
            continue                       # not ours, or relocatable already
        # The interpreter's own name, python3.12, from wherever it sat: the
        # path itself may hold spaces, so it is not split on them.
        found = re.search(r"/bin/(python[0-9.]*)", named)
        python = found.group(1) if found else "python3"
        script.write_text(_RELOCATABLE_SHEBANG.format(python=python)
                          + "\n".join(rest), encoding="utf-8")
        done.append(script.name)
    return done


def _verify_console_scripts(app: pathlib.Path) -> None:
    """No script in the bundle names the build machine's paths, and the CLI
    an agent is given really runs, from a clean environment."""
    runtime = app / "Contents" / "Resources" / "runtime"
    built_at = {str(app), str(app.resolve())}
    strays = []
    for script in sorted((runtime / "bin").iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        try:
            head = script.read_text(encoding="utf-8")[:1024]
        except (UnicodeDecodeError, OSError):
            continue
        if any(p in head for p in built_at):
            strays.append(script.name)
    if strays:
        raise SystemExit(
            "error: these scripts name the build directory and would fail "
            "on any other Mac:\n  " + "\n  ".join(strays))
    cli = runtime / "bin" / "compartment"
    if not cli.is_file():
        raise SystemExit("error: the bundle has no compartment command for "
                         "agents to run")
    with tempfile.TemporaryDirectory(prefix="compartment-buildprobe-") as tmp:
        out = subprocess.run([str(cli), "--version"], capture_output=True,
                             text=True, timeout=120,
                             env={"PATH": "/usr/bin:/bin", "HOME": tmp})
    if out.returncode != 0:
        raise SystemExit("error: the bundle's compartment command does not "
                         "run\n" + (out.stderr or out.stdout).strip()[:2000])
    print(f"the compartment command agents are given runs: "
          f"{out.stdout.strip()}")


def _unseal_hazards(contents: pathlib.Path) -> None:
    """Delete files that make the bundle unsignable.

    `venv` drops a `.gitignore` at the root of every environment it creates,
    which here is `Contents/`. codesign refuses to seal a stray dotfile in the
    bundle root, so the signature comes out structurally invalid:

        Compartment.app: code object is not signed at all
        In subcomponent: …/Contents/.gitignore

    An invalid signature is far worse than no signature. Gatekeeper reports
    `no usable signature`, and a quarantined copy - which is what everyone who
    downloads the .dmg has - is SIGKILLed the instant it execs. No dialog, no
    crash report, no dock bounce: the app simply does nothing when clicked.
    """
    for junk in (".gitignore", ".DS_Store"):
        p = contents / junk
        if p.exists():
            p.unlink()
            print(f"removed Contents/{junk} (would break the signature)")


def _verify_signature(app: pathlib.Path) -> None:
    """Refuse to ship a bundle whose signature does not verify.

    This is the check whose absence shipped 1.13.0 broken, so it is fatal
    rather than a warning.
    """
    out = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                          "--verbose=2", str(app)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(
            f"error: {app.name} is not correctly signed - refusing to ship it\n"
            f"{out.stderr.strip()}")
    print(f"signature verifies: {app.name}")


def _dylib_deps(binary: pathlib.Path) -> list[str]:
    out = subprocess.run(["otool", "-L", str(binary)],
                         capture_output=True, text=True, check=True)
    return [ln.strip().split(" (")[0]
            for ln in out.stdout.splitlines()[1:] if ln.strip()]


def _rewrite_dylib_refs(binary: pathlib.Path, name: str) -> None:
    """Point an absolute link at the copy inside the bundle.

    `cc -L<dir> -l<name>` records whatever install name the dylib carries, and
    a python-build-standalone libpython carries the absolute path it was built
    at - here, inside the build machine's uv directory. Left alone that is the
    every-app-only-runs-on-its-author's-Mac bug again, in a load command
    instead of a pyvenv.cfg.
    """
    for dep in _dylib_deps(binary):
        if dep.endswith("/" + name) and not dep.startswith("@"):
            _run("install_name_tool", "-change", dep, f"@rpath/{name}",
                 str(binary))


def _verify_no_external_dylibs(binary: pathlib.Path) -> None:
    """Nothing may be loaded from outside the bundle except the OS itself."""
    strays = [d for d in _dylib_deps(binary)
              if not d.startswith(("@", "/usr/lib/", "/System/"))]
    if strays:
        raise SystemExit(
            f"error: {binary.name} would load libraries from outside the "
            "bundle - it could not start on another Mac\n  "
            + "\n  ".join(strays))
    print(f"links nothing outside the bundle: {binary.name}")


def _verify_self_contained(app: pathlib.Path) -> None:
    """Prove the bundle needs nothing from outside itself.

    The check that was missing. The venv build shipped an app whose stdlib
    lived in the build machine's home directory; it started fine here and
    could not have started anywhere else, and no test said so.
    """
    contents = app / "Contents"
    runtime = contents / "Resources" / "runtime"
    probe = ("import json,sys,compartment;"
             "print(json.dumps({'prefix': sys.prefix, 'path': sys.path,"
             " 'compartment': compartment.__file__, 'version': compartment.__version__}))")
    out = subprocess.run(
        [str(runtime / "bin" / f"python{_EMBEDDED_VER[0]}"), "-c", probe],
        capture_output=True, text=True,
        env={"PYTHONHOME": str(runtime), "PATH": "/usr/bin:/bin",
             "HOME": os.environ.get("HOME", "/tmp")})
    if out.returncode != 0:
        raise SystemExit("error: the embedded interpreter cannot start\n"
                         + out.stderr.strip())
    import json as _json
    info = _json.loads(out.stdout)
    root = str(app.resolve())
    strays = [p for p in info["path"]
              if p and not str(pathlib.Path(p).resolve()).startswith(root)]
    if strays:
        raise SystemExit(
            "error: the bundle reaches outside itself - it would not start "
            "on another Mac\n  " + "\n  ".join(strays))
    if not str(pathlib.Path(info["compartment"]).resolve()).startswith(root):
        raise SystemExit(f"error: compartment loaded from {info['compartment']}")
    if not (runtime / "lib" / f"python{_EMBEDDED_VER[0]}" / "os.py").exists():
        raise SystemExit("error: no standard library inside the bundle")
    print(f"self-contained: compartment {info['version']}, "
          f"{len(info['path'])} sys.path entries, all inside the bundle")


def build_app(spec: str | None = None, out: pathlib.Path | None = None,
              python: str | None = None,
              min_macos: str = MIN_MACOS) -> pathlib.Path:
    """spec: what to pip-install (default: this checkout, so the build always
    matches the source it came from)."""
    version = _version()
    if _version_tuple(min_macos) < (13, 0):
        raise SystemExit("error: --min-macos cannot go below 13.0: the login "
                         "item is registered with SMAppService, which is "
                         "macOS 13 and up")
    base_python = python or find_standalone_python()
    arch = _python_arch(base_python)
    if arch not in ("arm64", "x86_64"):
        raise SystemExit(f"error: {base_python} is built for {arch!r}; the "
                         "app is built for arm64 or x86_64")
    print(f"embedding {base_python} ({arch}), for macOS {min_macos} and up")
    app = (out or BUILD / f"{APP_NAME}.app")
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # 1. the interpreter's ENTIRE runtime, stdlib included, under Resources -
    #    the one place codesign seals without extended attributes.
    ver = _python_version(base_python)
    _EMBEDDED_VER[0] = ver
    src_prefix = pathlib.Path(base_python).resolve().parent.parent
    if not (src_prefix / "lib" / f"python{ver}" / "os.py").exists():
        raise SystemExit(
            f"error: {base_python} has no self-contained standard library at "
            f"{src_prefix}\n  install one with:  uv python install 3.12")
    print(f"copying the Python {ver} runtime from {src_prefix}…")
    runtime = resources / "runtime"
    shutil.copytree(src_prefix, runtime, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  "*.pyo", ".DS_Store"))
    rt_python = runtime / "bin" / f"python{ver}"
    if not rt_python.exists():
        rt_python = runtime / "bin" / "python3"
    rt_python.chmod(0o755)
    # uv marks its interpreters PEP 668 externally-managed so nobody pip
    # installs into the shared copy. This one is a private copy inside the
    # bundle, and installing into it is the entire point.
    (runtime / "lib" / f"python{ver}" / "EXTERNALLY-MANAGED").unlink(
        missing_ok=True)
    if subprocess.run([str(rt_python), "-m", "pip", "--version"],
                      capture_output=True).returncode != 0:
        _run(rt_python, "-m", "ensurepip", "--default-pip")  # a pip-less build
    _install_packages(rt_python, spec or str(ROOT), arch, min_macos)
    fixed = _relocate_scripts(runtime)
    print(f"console scripts made relocatable: {', '.join(fixed) or 'none'}")

    _strip(runtime / "lib")

    # Precompile everything, and freeze it. Python writes __pycache__ next to
    # any module it imports without one - inside the bundle, after signing,
    # which breaks the app's own seal the first time it runs. Compiling ahead
    # with unchecked-hash means the interpreter trusts the bytecode without
    # even stat-ing the source, so there is nothing left to write. The
    # launcher sets PYTHONDONTWRITEBYTECODE too, belt and braces.
    print("precompiling the runtime…")
    cc = subprocess.run(
        [str(rt_python), "-m", "compileall", "-q", "-f",
         "--invalidation-mode", "unchecked-hash",
         str(runtime / "lib" / f"python{ver}")],
        capture_output=True, text=True)
    if cc.returncode != 0:                 # a few stdlib fixtures never compile
        print(f"  (compileall reported {cc.returncode}; continuing)")

    _unseal_hazards(contents)

    # 2. the executable named by Info.plist, compiled from tools/launcher.c.
    #    It embeds the interpreter with Py_BytesMain instead of exec'ing one,
    #    so the running image stays Contents/MacOS/Compartment for the life of
    #    the process. A shell launcher that exec's Contents/MacOS/python looks
    #    identical from inside - the status item reports itself visible, with
    #    an image and a window - but the menu bar never gives it a slot, and
    #    the icon simply never appears. See tools/launcher.c.
    launcher = macos / APP_NAME
    _run("cc", "-O2", "-arch", arch, f"-mmacosx-version-min={min_macos}",
         "-o", str(launcher), str(pathlib.Path(__file__).with_name("launcher.c")),
         f"-I{runtime / 'include' / f'python{ver}'}",
         f"-L{runtime / 'lib'}", f"-lpython{ver}",
         "-Wl,-rpath,@executable_path/../Resources/runtime/lib")
    launcher.chmod(0o755)
    _rewrite_dylib_refs(launcher, f"libpython{ver}.dylib")
    _verify_no_external_dylibs(launcher)

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
        "LSMinimumSystemVersion": min_macos,
        "NSHumanReadableCopyright": "https://github.com/"
                                    "MaxFreedomPollard/Compartment",
        "NSHumanReadableDescription": DESCRIPTION,
        "LSApplicationCategoryType": "public.app-category.productivity",
    }
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)

    # 4. ad-hoc signature, LAST - anything written into the bundle after this
    #    point invalidates the seal. Without a valid one macOS refuses to keep
    #    the login item, and the identifier is what System Settings groups the
    #    item under.
    # Checked BEFORE signing: running the interpreter is what writes bytecode
    # into the bundle, and doing that afterwards is what broke the seal.
    _verify_self_contained(app)
    _verify_console_scripts(app)
    _verify_deployment_target(app, arch, min_macos)

    _run("codesign", "--force", "--deep", "--sign", "-",
         "--identifier", BUNDLE_ID, app)
    _verify_signature(app)
    _no_xattr_signatures(app)
    _verify_survives_running(app)
    print(f"built {app}")
    return app


def _verify_survives_running(app: pathlib.Path) -> None:
    """A signed app must not invalidate itself by being used.

    Anything the app writes inside its own bundle - bytecode caches being the
    obvious one - breaks the seal, and a broken seal means a quarantined copy
    is killed on sight. So: run it once for real, then check the signature
    again.

    In a home of its own. `--self-check` with no vault named resolves
    DEFAULT_VAULT, which is the vault of whoever is building - so this step
    used to open, expiry-sweep and rewrite the developer's live memory vault,
    and its child CLI processes read the real boot-session credential, on
    every single build. A build is not allowed to touch the machine it runs
    on. HOME moves the vault, the settings and the session directory
    together; COMPARTMENT_VAULT is belt and braces for a HOME that some
    other layer has already resolved.
    """
    with tempfile.TemporaryDirectory(prefix="compartment-buildprobe-") as tmp:
        probe = pathlib.Path(tmp)
        out = subprocess.run(
            [str(app / "Contents" / "MacOS" / APP_NAME), "--self-check"],
            capture_output=True, text=True,
            env={**os.environ,
                 "HOME": str(probe),
                 "COMPARTMENT_VAULT": str(probe / "probe.vault"),
                 "COMPARTMENT_SESSION_DIR": str(probe / "session")})
    if out.returncode != 0:
        raise SystemExit("error: the built app cannot run\n"
                         + (out.stderr or out.stdout).strip()[:2000])
    _verify_signature(app)
    print("signature survives running the app")


def _no_xattr_signatures(app: pathlib.Path) -> None:
    """No file in the bundle may keep its signature in extended attributes.

    codesign does that for loose files in the bundle root, which it treats as
    nested code. Extended attributes do not survive hdiutil, zip, or cp, so
    such a bundle verifies where it was built and nowhere else.
    """
    bad = [p for p in app.rglob("*")
           if p.is_file() and not p.is_symlink()
           and b"com.apple.cs.CodeDirectory" in subprocess.run(
               ["xattr", str(p)], capture_output=True).stdout]
    if bad:
        rel = "\n  ".join(str(p.relative_to(app)) for p in bad[:10])
        raise SystemExit(
            "error: these files carry xattr-only signatures and would break "
            "the moment the app is copied:\n  " + rel)
    print("no xattr-only signatures: the bundle survives being copied")


def _login_plist(app: pathlib.Path) -> dict:
    return {
        "Label": BUNDLE_ID,
        "ProgramArguments": [str(pathlib.Path("/Applications") / app.name
                                 / "Contents" / "MacOS" / APP_NAME)],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }


def _app_arch(app: pathlib.Path) -> str:
    """The architecture of the built app, read from its executable."""
    slices = _macho_slices(app / "Contents" / "MacOS" / APP_NAME)
    if not slices:
        raise SystemExit(f"error: {app} has no executable to read")
    return ",".join(sorted(slices))


def artifact_name(version: str, arch: str, ext: str) -> str:
    """build/<this>: Compartment-<version>.pkg for Apple silicon, the build
    the README and every release so far have linked, and
    Compartment-<version>-intel.pkg for an Intel build. The two cannot share
    a name: a release carries both, and whichever was uploaded second would
    replace the first."""
    suffix = "" if arch == "arm64" else "-intel"
    return f"{APP_NAME}-{version}{suffix}.{ext}"


def _app_min_macos(app: pathlib.Path) -> str:
    """The minimum macOS the built app declares. The installers take it from
    the app rather than from a constant of their own, so a .pkg can never
    accept a macOS the app inside it was not built for."""
    with open(app / "Contents" / "Info.plist", "rb") as fh:
        return str(plistlib.load(fh).get("LSMinimumSystemVersion")
                   or MIN_MACOS)


def build_pkg(app: pathlib.Path) -> pathlib.Path:
    """A .pkg whose second, optional component starts it at login."""
    version = _version()
    min_macos = _app_min_macos(app)
    # The installer agrees to run only on the Macs this build can start on.
    # It used to say arm64,x86_64 whatever was inside, so an Intel Mac
    # installed the Apple silicon app - which could never launch, and being
    # a menu bar app said nothing about it.
    arch = _app_arch(app)
    staging = BUILD / "pkgroot"
    login_root = BUILD / "pkgroot-login"
    for d in (staging, login_root):
        if d.exists():
            shutil.rmtree(d)
    (staging / "Applications").mkdir(parents=True)
    # ditto, not copytree: it is the only copy that preserves every
    # attribute codesign relies on.
    _run("ditto", app, staging / "Applications" / app.name)
    login_root.mkdir(parents=True)          # payload-free: scripts only

    def _scripts(name: str, body: str) -> pathlib.Path:
        d = BUILD / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        post = d / "postinstall"
        post.write_text(body, encoding="utf-8")
        post.chmod(0o755)
        return d

    # Both scripts run as root. Anything that touches the user's GUI session -
    # SMAppService, `open` - has to be pushed back into it explicitly: root has
    # no per-user launchd bootstrap, and asking it to register a login item is
    # what produced "Could not connect to system service compartment". `$USER` is
    # not the installing human here either, so read the console owner instead.
    preamble = (
        "#!/bin/sh\n"
        f'APP="/Applications/{app.name}"\n'
        '[ -d "$APP" ] || exit 0\n'
        'USER_NAME=$(/usr/bin/stat -f%Su /dev/console)\n'
        'USER_UID=$(/usr/bin/id -u "$USER_NAME" 2>/dev/null)\n'
    )
    core_scripts = _scripts("core-scripts", preamble + (
        "# Installer payloads are not quarantined, but a bundle that arrived\n"
        "# any other way (the .dmg, Migration Assistant, a restored backup)\n"
        "# is, and a quarantined ad-hoc-signed app is SIGKILLed at exec.\n"
        '/usr/bin/xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true\n'
        "\n"
        "# A second copy under ~/Applications claims the same bundle id, and\n"
        "# then LaunchServices and SMAppService resolve whichever they saw\n"
        "# first - often the stale one.\n"
        'if [ -n "$USER_NAME" ] && [ "$USER_NAME" != "root" ]; then\n'
        '  /bin/rm -rf "/Users/$USER_NAME/Applications/Compartment.app" || true\n'
        "fi\n"
        'LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks'
        '/LaunchServices.framework/Support/lsregister"\n'
        '[ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1\n'
        "exit 0\n"))
    login_scripts = _scripts("login-scripts", preamble + (
        '[ -n "$USER_UID" ] && [ "$USER_NAME" != "root" ] || exit 0\n'
        "# Register with SMAppService so System Settings > Login Items lists\n"
        "# Compartment by name, with its icon, instead of a nameless legacy agent.\n"
        "asuser() { /bin/launchctl asuser \"$USER_UID\" /usr/bin/sudo -u "
        '"$USER_NAME" "$@"; }\n'
        'asuser "$APP/Contents/MacOS/Compartment" --login on || true\n'
        'asuser /usr/bin/open -a "$APP" || true\n'
        "exit 0\n"))

    core = BUILD / "core.pkg"
    login = BUILD / "login.pkg"
    _run("pkgbuild", "--root", staging, "--identifier", BUNDLE_ID,
         "--version", version, "--install-location", "/",
         "--scripts", core_scripts, core)
    _run("pkgbuild", "--root", login_root, "--identifier", BUNDLE_ID + ".login",
         "--version", version, "--install-location", "/",
         "--scripts", login_scripts, "--nopayload", login)

    dist = BUILD / "distribution.xml"
    dist.write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>Compartment</title>
  <options customize="always" require-scripts="false" hostArchitectures="{arch}"/>
  <volume-check><allowed-os-versions><os-version min="{min_macos}"/></allowed-os-versions></volume-check>
  <choices-outline>
    <line choice="core"/>
    <line choice="login"/>
  </choices-outline>
  <choice id="core" title="Compartment" description="The Compartment app, its encrypted memory vault, and the `compartment` command line tool."
          start_selected="true" start_enabled="false">
    <pkg-ref id="{BUNDLE_ID}"/>
  </choice>
  <choice id="login" title="Menu bar utility"
          description="Keep Compartment in the menu bar and start it automatically when you log in. You can turn this off later in System Settings &gt; General &gt; Login Items."
          start_selected="true">
    <pkg-ref id="{BUNDLE_ID}.login"/>
  </choice>
  <pkg-ref id="{BUNDLE_ID}" version="{version}">core.pkg</pkg-ref>
  <pkg-ref id="{BUNDLE_ID}.login" version="{version}">login.pkg</pkg-ref>
</installer-gui-script>
""", encoding="utf-8")

    out = BUILD / artifact_name(version, arch, "pkg")
    _run("productbuild", "--distribution", dist, "--package-path", BUILD, out)
    print(f"built {out}")
    return out


def build_dmg(app: pathlib.Path) -> pathlib.Path:
    version = _version()
    arch = _app_arch(app)
    stage = BUILD / "dmg"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    _run("ditto", app, stage / app.name)
    os.symlink("/Applications", stage / "Applications")
    # Compartment is signed ad-hoc, not with a paid Developer ID, so it cannot be
    # notarised. Anything dragged out of a downloaded .dmg carries the
    # quarantine flag, and macOS refuses to launch a quarantined app it cannot
    # attribute - silently, because Compartment has no dock icon to bounce. The
    # .pkg does not have this problem: installer payloads are not quarantined.
    (stage / "READ ME FIRST.txt").write_text(
        "Compartment " + version + "\n"
        "=================\n\n"
        "Easiest install: use " + artifact_name(version, arch, "pkg")
        + " from the release page\n"
        "instead of this disk image. It sets everything up in one click and\n"
        "skips the warning below entirely.\n\n"
        "If you would rather drag the app across:\n\n"
        "  1. Drag Compartment.app onto the Applications folder here.\n"
        "  2. The first time you open it, macOS will say it cannot check the\n"
        "     app for malicious software. That is what it always says about\n"
        "     software not signed with a $99/year Apple Developer ID.\n"
        "  3. Open System Settings > Privacy & Security, scroll down, and\n"
        "     click 'Open Anyway'. You only do this once.\n\n"
        "Compartment has no dock icon - it lives in the menu bar. If you cannot\n"
        "find its icon there (a full menu bar hides items behind the notch),\n"
        "just open Compartment.app again: it will show its panel in a window.\n\n"
        "Source, and every other way to install:\n"
        "https://github.com/MaxFreedomPollard/Compartment\n", encoding="utf-8")
    out = BUILD / artifact_name(version, arch, "dmg")
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
    ap.add_argument("--min-macos", default=MIN_MACOS,
                    help=f"oldest macOS the app runs on (default {MIN_MACOS}); "
                         "every wheel is chosen for it")
    ap.add_argument("--dmg", action="store_true")
    ap.add_argument("--pkg", action="store_true")
    args = ap.parse_args()
    BUILD.mkdir(exist_ok=True)
    app = build_app(args.spec, python=args.python, min_macos=args.min_macos)
    if args.dmg:
        build_dmg(app)
    if args.pkg:
        build_pkg(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
