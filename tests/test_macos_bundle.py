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
    src = SPEC.read_text(encoding="utf-8")
    assert "PYTHONDONTWRITEBYTECODE=1" in src
    assert "compileall" in src and "unchecked-hash" in src


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
