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
