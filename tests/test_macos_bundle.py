"""The bundle has to be signable.

Compartment 1.13.0 shipped an app that did nothing at all when clicked. `venv`
leaves a `.gitignore` at the root of every environment it creates - here
`Compartment.app/Contents/` - and codesign will not seal a stray dotfile in the
bundle root, so the signature came out structurally invalid. Gatekeeper then
reads a quarantined copy as having "no usable signature" and the kernel
SIGKILLs it the moment it execs: no dialog, no crash report, nothing.

Nothing tested the bundle, so nothing caught it. These do.
"""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "tools" / "build_macos_app.py"
LAUNCHER = ROOT / "tools" / "launcher.c"


def _builder():
    spec = importlib.util.spec_from_file_location("build_macos_app", SPEC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_macos_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_unseal_hazards_removes_the_venv_gitignore(tmp_path):
    contents = tmp_path / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / ".gitignore").write_text(
        "# Created by venv; see https://docs.python.org/3/library/venv.html\n*\n")
    (contents / ".DS_Store").write_text("junk")
    (contents / "Info.plist").write_text("<plist/>")

    _builder()._unseal_hazards(contents)

    assert not (contents / ".gitignore").exists()
    assert not (contents / ".DS_Store").exists()
    assert (contents / "Info.plist").exists(), "must not touch real payload"


def test_unseal_hazards_is_safe_on_a_clean_bundle(tmp_path):
    contents = tmp_path / "Contents"
    contents.mkdir()
    _builder()._unseal_hazards(contents)          # must not raise


@pytest.mark.skipif(sys.platform != "darwin", reason="codesign is macOS only")
def test_verify_signature_rejects_an_unsigned_bundle(tmp_path):
    """The guard that would have stopped 1.13.0 going out."""
    app = tmp_path / "Fake.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("<plist/>")
    with pytest.raises(SystemExit, match="not correctly signed"):
        _builder()._verify_signature(app)


def test_installer_never_registers_the_login_item_as_root():
    """Root has no GUI launchd bootstrap, so SMAppService there fails with
    "Could not connect to system service" - the error the user saw."""
    src = SPEC.read_text(encoding="utf-8")
    assert 'su "$USER"' not in src, "$USER is root in a pkg script"
    assert "stat -f%Su /dev/console" in src, "must find the real console user"
    assert "launchctl asuser" in src, "must re-enter the user's GUI session"


def test_installer_strips_quarantine_and_stale_copies():
    src = SPEC.read_text(encoding="utf-8")
    assert "xattr -dr com.apple.quarantine" in src
    assert "Applications/Compartment.app" in src and "/bin/rm -rf" in src


# ------------------------------------------------- the bundle must stand alone

def test_no_venv_the_app_must_carry_its_own_stdlib():
    """A venv has no standard library. It has a pyvenv.cfg pointing at the
    interpreter it was built from - on the *build machine*. The app that
    shipped that way started here and could not start anywhere else."""
    src = SPEC.read_text(encoding="utf-8")
    assert '"-m", "venv"' not in src and "'-m', 'venv'" not in src
    assert "PYTHONHOME" in src, "the runtime is located by PYTHONHOME now"
    assert 'resources / "runtime"' in src, "the runtime belongs under Resources"


def test_launcher_freezes_the_bundle_at_runtime():
    """Bytecode written into the bundle after signing breaks its own seal,
    and a broken seal is a silent SIGKILL for any quarantined copy."""
    assert 'setenv("PYTHONDONTWRITEBYTECODE", "1", 1)' in LAUNCHER.read_text(
        encoding="utf-8")
    src = SPEC.read_text(encoding="utf-8")
    assert "compileall" in src and "unchecked-hash" in src


# --------------------------------------------- the icon has to reach the bar

def test_the_launcher_runs_python_in_process_and_never_execs_another_binary():
    """The bug this replaced: a shell launcher that exec'd Contents/MacOS/python
    left the running image no longer matching CFBundleExecutable. From inside
    the process everything looked right - the status item reported itself
    visible, with an image and a window - but the menu bar never gave it a
    slot, so the icon simply never appeared. Launching the same bundle from a
    shell worked, which is what made it so slow to find."""
    c = LAUNCHER.read_text(encoding="utf-8")
    assert "Py_BytesMain" in c, "the interpreter must run inside this process"
    assert "exec" not in c.split("*/")[-1], "no exec of a second binary"
    src = SPEC.read_text(encoding="utf-8")
    assert 'macos / "python"' not in src, "nothing but the launcher in MacOS/"
    assert '"cc", "-O2"' in src, "the launcher is compiled, not written as sh"
    assert 'exec "$here/python"' not in src


def test_the_launcher_forwards_its_arguments():
    """--render, --login and --show all arrive through the bundle executable;
    the pkg's postinstall calls it with --login on."""
    c = LAUNCHER.read_text(encoding="utf-8")
    assert "for (int i = 1; i < argc; i++)" in c
    assert "-psn_" in c, "LaunchServices' process serial number is not ours"


def test_the_launcher_behaves_like_the_interpreter_for_m_and_c():
    """Inside the bundle this binary is sys.executable, and
    [sys.executable, "-m", pkg] is how Python re-enters its own code.

    While the launcher forced `compartment.cli menubar` onto every
    invocation, each of those calls landed in argparse instead: the panel
    could not read its own vault status, and read the "[--keyfile KEYFILE]"
    out of argparse's usage line as a vault demanding a 2FA keyfile that had
    never been set up."""
    c = LAUNCHER.read_text(encoding="utf-8")
    assert 'strcmp(user[0], "-m")' in c and 'strcmp(user[0], "-c")' in c, \
        "-m and -c must pass through untouched"
    body = c.split("int main")[1]
    forced = body.index('"menubar"')
    guard = body.index('strcmp(user[0], "-m")')
    assert guard < forced, "the menubar subcommand must be conditional"


@pytest.mark.skipif(sys.platform != "darwin", reason="the bundle is macOS")
@pytest.mark.parametrize("bundle", [
    pathlib.Path("/Applications/Compartment.app"),
    ROOT / "build" / "Compartment.app",
])
def test_a_built_launcher_really_runs_as_an_interpreter(bundle):
    """The source assertion above cannot prove the compiled binary agrees."""
    exe = bundle / "Contents" / "MacOS" / "Compartment"
    if not exe.is_file():
        pytest.skip(f"no bundle at {bundle}")
    import subprocess
    r = subprocess.run([str(exe), "-c", "import sys; print(sys.prefix)"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[:400]
    assert "Resources/runtime" in r.stdout, r.stdout

    v = subprocess.run([str(exe), "-m", "compartment.cli", "--version"],
                       capture_output=True, text=True, timeout=120)
    assert v.returncode == 0, v.stderr[:400]
    assert "usage:" not in (v.stdout + v.stderr).lower(), \
        "the CLI must run, not print argparse usage"


def test_the_launcher_links_nothing_from_the_build_machine(tmp_path):
    """cc records whatever install name a dylib carries, and a standalone
    libpython carries the absolute path it was built at - inside the build
    machine's home. That is the app-only-runs-for-its-author bug again, in a
    load command instead of a pyvenv.cfg."""
    b = _builder()
    fake = tmp_path / "Compartment"
    fake.write_bytes(b"")
    b._dylib_deps = lambda _p: ["/usr/lib/libSystem.B.dylib",
                                "@rpath/libpython3.13.dylib"]
    b._verify_no_external_dylibs(fake)                     # must not raise
    b._dylib_deps = lambda _p: [
        "/Users/someone/.local/share/uv/python/x/lib/libpython3.13.dylib"]
    with pytest.raises(SystemExit) as e:
        b._verify_no_external_dylibs(fake)
    assert "outside the bundle" in str(e.value)


def test_bundle_copies_use_ditto():
    """cp and copytree drop metadata codesign depends on."""
    src = SPEC.read_text(encoding="utf-8")
    assert '_run("ditto", app,' in src
    assert "shutil.copytree(app," not in src


def test_the_build_refuses_to_ship_a_broken_bundle():
    """Every guard that would have caught what 1.13.0 shipped."""
    src = SPEC.read_text(encoding="utf-8")
    for guard in ("_verify_signature", "_verify_self_contained",
                  "_no_xattr_signatures", "_verify_survives_running"):
        assert f"def {guard}" in src, f"{guard} is missing"
        assert f"    {guard}(app)" in src, f"{guard} is never called"


def test_a_system_python_is_never_embedded():
    """The whole prefix gets copied in, so /usr would mean copying /usr."""
    src = SPEC.read_text(encoding="utf-8")
    assert '("/usr", "/System", "/Library")' in src


# ------------------------------------- every binary runs where the app claims

def _macho(arch="arm64", minos=(13, 0), legacy=False) -> bytes:
    """A minimal 64-bit Mach-O header with the one load command that names
    its minimum macOS: LC_BUILD_VERSION, or the older LC_VERSION_MIN_MACOSX."""
    import struct
    cpu = {"arm64": 0x0100000C, "x86_64": 0x01000007}[arch]
    v = (minos[0] << 16) | (minos[1] << 8)
    cmd = (struct.pack("<IIII", 0x24, 16, v, v) if legacy
           else struct.pack("<IIIIII", 0x32, 24, 1, v, v, 0))
    return struct.pack("<4sIiIIIII", b"\xcf\xfa\xed\xfe", cpu, 0, 6, 1,
                       len(cmd), 0, 0) + cmd


def _fat(*slices) -> bytes:
    import struct
    cpus = {"arm64": 0x0100000C, "x86_64": 0x01000007}
    entries, body = b"", b""
    for arch, data in slices:
        entries += struct.pack(">IIIII", cpus[arch], 0, 4096 + len(body),
                               len(data), 12)
        body += data.ljust(4096, b"\0")
    head = struct.pack(">II", 0xCAFEBABE, len(slices)) + entries
    return head.ljust(4096, b"\0") + body


def test_the_mach_o_reader_finds_each_slice_and_its_minimum(tmp_path):
    b = _builder()
    thin = tmp_path / "thin.so"
    thin.write_bytes(_macho("arm64", (14, 0)))
    assert b._macho_slices(thin) == {"arm64": (14, 0)}
    old = tmp_path / "old.so"
    old.write_bytes(_macho("x86_64", (10, 9), legacy=True))
    assert b._macho_slices(old) == {"x86_64": (10, 9)}
    fat = tmp_path / "fat.so"
    fat.write_bytes(_fat(("x86_64", _macho("x86_64", (10, 13))),
                         ("arm64", _macho("arm64", (11, 0)))))
    assert b._macho_slices(fat) == {"x86_64": (10, 13), "arm64": (11, 0)}
    # Not Mach-O: text, and a Java class file, which shares the fat magic.
    (tmp_path / "a.py").write_text("print('hi')\n")
    (tmp_path / "A.class").write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34")
    assert b._macho_slices(tmp_path / "a.py") == {}
    assert b._macho_slices(tmp_path / "A.class") == {}


def _fake_app(tmp_path, files: dict) -> pathlib.Path:
    app = tmp_path / "Compartment.app"
    for rel, data in files.items():
        p = app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return app


def test_a_library_built_for_a_newer_macos_than_the_app_is_refused(tmp_path):
    """onnxruntime 1.24 and later ship macOS wheels tagged macosx_14_0 only,
    and pip on a current build machine takes them, so the bundle declaring
    13.0 carried an embedding model that macOS 13 cannot load."""
    lib = ("Contents/Resources/runtime/lib/python3.12/site-packages/"
           "onnxruntime/capi/onnxruntime_pybind11_state.so")
    app = _fake_app(tmp_path, {
        "Contents/MacOS/Compartment": _macho("arm64", (13, 0)),
        lib: _macho("arm64", (14, 0)),
        "Contents/Resources/runtime/lib/python3.12/os.py": b"# stdlib\n"})
    b = _builder()
    with pytest.raises(SystemExit) as e:
        b._verify_deployment_target(app, "arm64", "13.0")
    assert "onnxruntime_pybind11_state.so needs macOS 14.0" in str(e.value)
    b._verify_deployment_target(app, "arm64", "14.0")        # must not raise


def test_an_intel_build_refuses_a_library_without_an_intel_slice(tmp_path):
    app = _fake_app(tmp_path, {
        "Contents/MacOS/Compartment": _macho("x86_64", (13, 0)),
        "Contents/Resources/runtime/lib/x.so": _macho("arm64", (11, 0)),
        "Contents/Resources/runtime/lib/u.so": _fat(
            ("x86_64", _macho("x86_64", (10, 13))),
            ("arm64", _macho("arm64", (11, 0))))})
    b = _builder()
    with pytest.raises(SystemExit) as e:
        b._verify_deployment_target(app, "x86_64", "13.0")
    assert "x.so (arm64)" in str(e.value)
    assert "u.so" not in str(e.value), "a universal2 library is fine"


def test_the_build_runs_the_deployment_check_before_signing():
    src = SPEC.read_text(encoding="utf-8")
    body = src.split("def build_app")[1]
    assert body.index("_verify_deployment_target(app, arch, min_macos)") \
        < body.index('_run("codesign"')


def test_wheels_are_chosen_for_the_declared_minimum_macos(tmp_path,
                                                          monkeypatch):
    """pip on its own picks wheels for the macOS it runs on. The build has to
    ask for the macOS the app declares, binary only, and install nothing
    else."""
    import subprocess
    b = _builder()
    b._EMBEDDED_VER[0] = "3.12"
    calls = []

    def fake_run(*args, **kw):
        argv = [str(a) for a in args]
        calls.append(argv)
        if "wheel" in argv:
            house = pathlib.Path(argv[argv.index("--wheel-dir") + 1])
            (house / "compartment-4.13.0-py3-none-any.whl").write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0)

    def fake_subprocess_run(argv, **kw):
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(b, "_run", fake_run)
    monkeypatch.setattr(b.subprocess, "run", fake_subprocess_run)
    b._install_packages(pathlib.Path("/rt/bin/python3.12"), "/src",
                        "arm64", "13.0")
    download = next(c for c in calls if "download" in c)
    assert download[download.index("--platform") + 1] == "macosx_13_0_arm64"
    assert "--only-binary=:all:" in download
    install = next(c for c in calls if "install" in c)
    assert "--no-index" in install, "only the wheels chosen above"
    assert not any("install" in c and "--no-index" not in c for c in calls)


def test_no_wheels_for_the_minimum_says_what_to_embed_instead(tmp_path,
                                                             monkeypatch):
    import subprocess
    b = _builder()
    b._EMBEDDED_VER[0] = "3.14"

    def fake_run(*args, **kw):
        argv = [str(a) for a in args]
        if "wheel" in argv:
            house = pathlib.Path(argv[argv.index("--wheel-dir") + 1])
            (house / "compartment-4.13.0-py3-none-any.whl").write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(b, "_run", fake_run)
    monkeypatch.setattr(b.subprocess, "run", lambda argv, **kw: (
        subprocess.CompletedProcess(argv, 1, "", "ERROR: No matching "
                                    "distribution found for onnxruntime")))
    with pytest.raises(SystemExit) as e:
        b._install_packages(pathlib.Path("/rt/bin/python3.14"), "/src",
                            "arm64", "13.0")
    msg = str(e.value)
    assert "Python 3.14 on macOS 13.0" in msg and "onnxruntime" in msg
    assert "--min-macos 14.0" in msg and "3.12" in msg


def test_the_installer_accepts_only_the_macos_the_app_declares(tmp_path,
                                                              monkeypatch):
    import plistlib
    b = _builder()
    app = tmp_path / "Compartment.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"LSMinimumSystemVersion": "14.0"}, fh)
    monkeypatch.setattr(b, "BUILD", tmp_path / "build")
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    b.build_pkg(app)
    dist = (tmp_path / "build" / "distribution.xml").read_text()
    assert '<os-version min="14.0"/>' in dist


def test_a_minimum_below_what_the_login_item_needs_is_refused():
    with pytest.raises(SystemExit, match="SMAppService"):
        _builder().build_app(python="/nonexistent", min_macos="12.0")


# ---------------------------- the CLI inside the bundle runs on another Mac

def _pip_written(runtime: pathlib.Path, name: str, long_form=False) -> None:
    """A console script as pip writes it: the absolute path of the
    interpreter that installed it, plain or in the exec form pip uses for a
    path with a space in it."""
    py = runtime / "bin" / "python3.12"
    head = (f"#!/bin/sh\n'''exec' \"{py}\" \"$0\" \"$@\"\n' '''\n"
            if long_form else f"#!{py}\n")
    (runtime / "bin" / name).write_text(
        head + "# -*- coding: utf-8 -*-\nimport sys\n"
        "print('ran', sys.argv[1:])\n", encoding="utf-8")
    (runtime / "bin" / name).chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="the bundle is POSIX")
def test_console_scripts_still_run_after_the_bundle_moves(tmp_path):
    """pip names the build directory's interpreter in every script it writes,
    and `compartment` inside the app is what the panel writes into each
    agent's config. Installed anywhere else - which is everywhere but the
    build machine - it failed with "bad interpreter"."""
    import shutil
    import subprocess
    if not shutil.which("realpath"):
        pytest.skip("no realpath here")
    build = tmp_path / "build dir" / "runtime"
    (build / "bin").mkdir(parents=True)
    (build / "bin" / "python3.12").symlink_to(sys.executable)
    _pip_written(build, "compartment")
    _pip_written(build, "cmpt", long_form=True)
    relocatable = "#!/bin/sh\n'''exec' \"$(dirname -- \"$0\")/python3.12\" \"$0\"\n' '''\nprint(1)\n"
    (build / "bin" / "pip").write_text(relocatable, encoding="utf-8")
    assert sorted(_builder()._relocate_scripts(build)) == ["cmpt",
                                                           "compartment"]
    assert (build / "bin" / "pip").read_text() == relocatable, \
        "a script that is relocatable already is left alone"
    installed = tmp_path / "Applications" / "runtime"
    installed.parent.mkdir()
    shutil.move(str(build), str(installed))
    for name in ("compartment", "cmpt"):
        script = installed / "bin" / name
        assert str(build) not in script.read_text()
        r = subprocess.run([str(script), "--version"], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ran ['--version']"


def test_a_script_naming_the_build_directory_fails_the_build(tmp_path):
    app = tmp_path / "Compartment.app"
    runtime = app / "Contents" / "Resources" / "runtime"
    (runtime / "bin").mkdir(parents=True)
    _pip_written(runtime, "compartment")
    with pytest.raises(SystemExit, match="name the build directory"):
        _builder()._verify_console_scripts(app)


def test_the_build_makes_its_scripts_relocatable_and_checks_them():
    src = SPEC.read_text(encoding="utf-8")
    body = src.split("def build_app")[1]
    assert body.index("_relocate_scripts(runtime)") < body.index("compileall")
    assert body.index("_verify_console_scripts(app)") \
        < body.index('_run("codesign"')
