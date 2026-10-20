"""The command written into every client's config has to start the server
from a process that has none of the user's shell: a GUI app launched from
the Dock or the Start menu, or the panel's own child process.

These are about where that command is found, for the ways Compartment is
actually installed: `pip install --user` (what the install scripts run), a
venv, and a package manager that keeps a link on PATH to a versioned tree.
"""
import sys
import sysconfig

import pytest

from compartment import clients


def _fake_schemes(monkeypatch, tmp_path, installed_in: str):
    """Three install schemes whose directories are under tmp_path, and this
    package imported from the library directory of `installed_in`."""
    layout = {}
    for scheme in ("venv", "posix_prefix", "posix_user", "osx_framework_user",
                   "nt", "nt_user"):
        lib = tmp_path / scheme / "site-packages"
        layout[scheme] = {"purelib": str(lib), "platlib": str(lib),
                          "scripts": str(tmp_path / scheme / "bin")}

    def get_paths(scheme=None, vars=None, expand=True):
        if scheme not in layout:
            raise KeyError(scheme)
        return dict(layout[scheme])

    lib = tmp_path / installed_in / "site-packages"
    lib.mkdir(parents=True)
    monkeypatch.setattr(sysconfig, "get_paths", get_paths)
    monkeypatch.setattr(clients, "_package_root", lambda: lib.resolve(),
                        raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "interpreter"))
    monkeypatch.setattr(sys, "executable",
                        str(tmp_path / "interpreter" / "bin" / "python3"))
    monkeypatch.setattr(clients.shutil, "which", lambda *a, **k: None)
    return {k: tmp_path / k / "bin" for k in layout}


def _script(directory, name="compartment"):
    directory.mkdir(parents=True, exist_ok=True)
    s = directory / name
    s.write_text("#!/bin/sh\n", encoding="utf-8")
    return s


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
def test_a_user_install_names_the_script_pip_wrote(platform, tmp_path,
                                                   monkeypatch):
    """`pip install --user` (every install script) puts the script in the
    user scheme's bin or Scripts directory, beside no interpreter and on no
    PATH a Dock-launched app or the panel's child has. Only the interpreter's
    own directories and PATH were looked in, so the bare name was written,
    and every GUI client failed with "spawn compartment ENOENT"."""
    user = {"macos": "osx_framework_user", "windows": "nt_user",
            "linux": "posix_user"}[platform]
    monkeypatch.setattr(sysconfig, "get_default_scheme",
                        lambda: "nt" if platform == "windows"
                        else "posix_prefix")
    monkeypatch.setattr(sysconfig, "get_preferred_scheme",
                        lambda key: user if key == "user"
                        else ("nt" if platform == "windows"
                              else "posix_prefix"))
    bins = _fake_schemes(monkeypatch, tmp_path, installed_in=user)
    name = "compartment.exe" if platform == "windows" else "compartment"
    script = _script(bins[user], name)
    # A stale copy in a scheme this package is not installed in is not it.
    _script(bins["posix_prefix" if platform != "windows" else "nt"], name)

    assert clients.executable() == str(script)


def test_a_venv_install_is_still_named_by_its_venv_script(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(sysconfig, "get_default_scheme", lambda: "venv")
    monkeypatch.setattr(sysconfig, "get_preferred_scheme",
                        lambda key: "posix_user" if key == "user" else "venv")
    bins = _fake_schemes(monkeypatch, tmp_path, installed_in="venv")
    script = _script(bins["venv"])
    _script(bins["posix_user"])            # an older --user copy, not this one
    assert clients.executable() == str(script)


def _symlink_or_skip(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"no symlinks here ({exc})")


def test_a_package_managers_link_is_written_instead_of_its_versioned_tree(
        tmp_path, monkeypatch):
    """Homebrew runs a Python formula from Cellar/<name>/<version>/libexec,
    so the script beside the interpreter has the version in its path and
    is gone after the next `brew upgrade` and cleanup: every client config
    written before then pointed at a file that no longer existed. The link
    Homebrew keeps in its bin directory is repointed by every upgrade."""
    prefix = tmp_path / "Cellar" / "compartment" / "4.12.0" / "libexec"
    script = _script(prefix / "bin")
    link = tmp_path / "homebrew" / "bin" / "compartment"
    _symlink_or_skip(link, script)
    monkeypatch.setattr(clients, "_package_root",
                        lambda: tmp_path / "not-a-scheme", raising=False)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(clients, "_link_dirs", lambda: [], raising=False)
    monkeypatch.setattr(clients.shutil, "which", lambda *a, **k: str(link))

    assert clients.executable() == str(link)


def test_a_link_to_some_other_install_is_not_taken_for_this_one(
        tmp_path, monkeypatch):
    """A `compartment` on PATH that is another copy (an old --user install
    while this one runs from a venv) is not the one to name."""
    prefix = tmp_path / "venv"
    script = _script(prefix / "bin")
    other = _script(tmp_path / "old-install" / "bin")
    link = tmp_path / "local" / "bin" / "compartment"
    _symlink_or_skip(link, other)
    monkeypatch.setattr(clients, "_package_root",
                        lambda: tmp_path / "not-a-scheme", raising=False)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(clients, "_link_dirs", lambda: [link.parent],
                        raising=False)
    monkeypatch.setattr(clients.shutil, "which", lambda *a, **k: str(link))

    assert clients.executable() == str(script)


def test_a_link_in_a_package_managers_bin_off_path_is_found(tmp_path,
                                                           monkeypatch):
    """The panel's child process has the login shell's PATH at best, which
    may not hold ~/.local/bin; pipx's and uv's link there is found anyway."""
    prefix = tmp_path / "pipx" / "venvs" / "compartment"
    script = _script(prefix / "bin")
    link = tmp_path / "home" / ".local" / "bin" / "compartment"
    _symlink_or_skip(link, script)
    monkeypatch.setattr(clients, "_package_root",
                        lambda: tmp_path / "not-a-scheme", raising=False)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(clients.Path, "home",
                        classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(clients.shutil, "which", lambda *a, **k: None)

    assert clients.executable() == str(link)
