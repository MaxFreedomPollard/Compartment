"""Where `integrate hermes`, `uninstall` and the refresh after an update look
for Hermes Agent: where Hermes itself looks, which on native Windows is
%LOCALAPPDATA%\\hermes and not ~/.hermes."""
import subprocess
import sys
import types

import pytest

from compartment import cli


@pytest.fixture()
def windows_hermes(tmp_path, monkeypatch):
    """Windows, with Hermes installed by its own installer: no HERMES_HOME
    exported, its home under LOCALAPPDATA and its venv in the Windows
    layout. Every program it would run is a stub that records the call."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(cli.Path, "home",
                        classmethod(lambda cls: tmp_path / "home"))
    venv_py = local / "hermes" / "hermes-agent" / "venv" / "Scripts" / \
        "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"MZ")
    ran = []

    def run(argv, **kwargs):
        ran.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: None)
    vault = tmp_path / "memory.vault"
    vault.write_bytes(b"")
    return types.SimpleNamespace(home=local / "hermes", venv_py=venv_py,
                                 ran=ran, vault=str(vault),
                                 wrong=tmp_path / "home" / ".hermes")


def test_integrate_hermes_on_windows_writes_where_hermes_reads(
        windows_hermes, capsys):
    """The plugin went to ~/.hermes/plugins, which Hermes on Windows never
    reads, the Hermes venv was looked for as bin/python and never found,
    and the install was reported done."""
    cli._integrate_target(types.SimpleNamespace(target="hermes",
                                                vault=windows_hermes.vault))
    plugin = windows_hermes.home / "plugins" / "compartment"
    assert (plugin / "plugin.yaml").is_file()
    assert (plugin / "__init__.py").is_file()
    assert not windows_hermes.wrong.exists()
    assert [str(windows_hermes.venv_py), "-c", "import compartment"] in \
        windows_hermes.ran


def test_hermes_home_follows_hermes_rules(tmp_path, monkeypatch):
    """HERMES_HOME with its variables expanded, as Hermes expands it; else
    ~/.hermes, and on Windows %LOCALAPPDATA%\\hermes."""
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HERMES_BASE", str(tmp_path / "base"))
    monkeypatch.setenv("HERMES_HOME", "$HERMES_BASE/custom-hermes")
    assert cli._hermes_home() == tmp_path / "base" / "custom-hermes"
    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli._hermes_home() == tmp_path / ".hermes"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert cli._hermes_home() == tmp_path / "AppData" / "Local" / "hermes"
