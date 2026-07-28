"""macOS has to be able to name and draw the login item.

A pip install registered a LaunchAgent that runs a bare executable. System
Settings has no bundle to read, so Login Items listed Compartment as a blank
page, and App Background Activity listed a lowercase "compartment" from an
unidentified developer. macOS takes both the name and the icon from an
application bundle, so a pip install writes itself a small one.
"""
import plistlib
import subprocess
import sys

import pytest

from compartment import menubar

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="login items are macOS")


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    b = tmp_path / "Applications" / "Compartment.app"
    monkeypatch.setattr(menubar, "USER_APP_BUNDLE", b)
    monkeypatch.setattr(menubar, "installed_app_bundle", lambda: None)
    return b


def _plist(b):
    return plistlib.loads((b / "Contents" / "Info.plist").read_bytes())


def test_it_writes_a_bundle_macos_can_read(bundle):
    out = menubar.ensure_login_bundle()
    assert out == bundle
    assert (bundle / "Contents" / "Info.plist").is_file()
    assert (bundle / "Contents" / "MacOS" / "Compartment").is_file()


def test_the_name_shown_is_compartment(bundle):
    menubar.ensure_login_bundle()
    info = _plist(bundle)
    assert info["CFBundleName"] == "Compartment"
    assert info["CFBundleDisplayName"] == "Compartment"


def test_it_carries_an_icon(bundle):
    menubar.ensure_login_bundle()
    assert _plist(bundle)["CFBundleIconFile"] == "app"
    assert (bundle / "Contents" / "Resources" / "app.icns").is_file()


def test_the_icon_ships_with_the_package():
    icns = menubar.Path(menubar.__file__).resolve().parent / "data" / "app.icns"
    assert icns.is_file()
    assert icns.read_bytes()[:4] == b"icns"


def test_it_does_not_bounce_in_the_dock(bundle):
    """A menu bar app with a Dock icon and no window is a nuisance."""
    menubar.ensure_login_bundle()
    assert _plist(bundle)["LSUIElement"] is True


def test_the_launcher_starts_the_panel(bundle):
    menubar.ensure_login_bundle()
    script = (bundle / "Contents" / "MacOS" / "Compartment").read_text()
    assert script.startswith("#!/bin/sh")
    assert "menubar" in script
    assert script.strip().endswith('menubar "$@"')


def test_the_launcher_is_executable(bundle):
    menubar.ensure_login_bundle()
    assert (bundle / "Contents" / "MacOS" / "Compartment").stat().st_mode & 0o111


def test_the_login_item_points_at_the_bundle(bundle):
    """The whole point: the agent must run the bundle, not the bare script."""
    menubar.ensure_login_bundle()
    argv = menubar._launcher_argv()
    assert argv == [str(bundle / "Contents" / "MacOS" / "Compartment")]


def test_it_signs_so_macos_stops_calling_it_unidentified(bundle):
    menubar.ensure_login_bundle()
    r = subprocess.run(["codesign", "--verify", "--strict", str(bundle)],
                       capture_output=True, text=True, timeout=60)
    if "command not found" in (r.stderr or ""):
        pytest.skip("no codesign on this machine")
    assert r.returncode == 0, r.stderr


def test_nothing_unexpected_sits_directly_under_contents(bundle):
    """codesign walks the bundle and refuses to seal a stray file under
    Contents, which is how the first attempt stayed unsigned."""
    menubar.ensure_login_bundle()
    loose = [p.name for p in (bundle / "Contents").iterdir() if p.is_file()]
    assert loose == ["Info.plist"]


def test_a_real_installed_app_is_used_instead_of_writing_one(tmp_path,
                                                             monkeypatch):
    """Someone who ran the .pkg already has the signed article. Never write
    a second bundle beside it."""
    real = tmp_path / "Applications" / "Compartment.app"
    (real / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(menubar, "installed_app_bundle", lambda: real)
    written = tmp_path / "user" / "Compartment.app"
    monkeypatch.setattr(menubar, "USER_APP_BUNDLE", written)
    assert menubar.ensure_login_bundle() == real
    assert not written.exists()


def test_a_generated_bundle_is_recognisable(bundle):
    menubar.ensure_login_bundle()
    assert menubar._is_generated(bundle) is True


def test_an_installed_bundle_is_not_mistaken_for_a_generated_one(tmp_path):
    real = tmp_path / "Compartment.app"
    (real / "Contents" / "MacOS").mkdir(parents=True)
    (real / "Contents" / "MacOS" / "Compartment").write_text("")
    assert menubar._is_generated(real) is False


def test_writing_it_twice_is_harmless(bundle):
    menubar.ensure_login_bundle()
    first = (bundle / "Contents" / "Info.plist").read_bytes()
    assert menubar.ensure_login_bundle() == bundle
    assert (bundle / "Contents" / "Info.plist").read_bytes() == first


def test_an_unwritable_applications_folder_falls_back(monkeypatch, tmp_path):
    """No bundle is a plain login item, which is what it was before. It must
    never be a failed install."""
    monkeypatch.setattr(menubar, "installed_app_bundle", lambda: None)
    monkeypatch.setattr(menubar, "USER_APP_BUNDLE", tmp_path / "x.app")

    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(menubar.Path, "mkdir", boom)
    assert menubar.ensure_login_bundle() is None
    argv = menubar._launcher_argv()
    assert argv and argv[0].endswith(("compartment", "python3", "python"))
