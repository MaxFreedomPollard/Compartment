"""Which tool installed this copy, so that `update` upgrades it with that
tool and `uninstall` names that tool's command for the last step."""
import subprocess
import sys
import types

import pytest

from compartment import cli


@pytest.fixture()
def env_root(tmp_path, monkeypatch):
    """sys.prefix an environment of this test's own, and nothing on PATH or
    in argv that points into a tool's directory."""
    root = tmp_path / "env"
    root.mkdir()
    monkeypatch.setattr(sys, "prefix", str(root))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "bin" / "compartment")])
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    return root


def test_a_uv_tool_install_on_windows_is_upgraded_with_uv(env_root,
                                                         monkeypatch):
    """uv's launcher on Windows is an .exe of its own in ~/.local/bin, not a
    symlink into uv's tools directory, and its paths have backslashes: the
    "uv/tools" test never matched there, and `update` ran `python -m pip`
    in an environment uv builds without pip."""
    (env_root / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "argv",
                        [r"C:\Users\me\.local\bin\compartment.exe"])
    assert cli._install_kind() == ("uv", ["uv", "tool", "upgrade",
                                          "compartment"])


def test_a_pipx_install_is_upgraded_and_removed_with_pipx(env_root,
                                                         monkeypatch, capsys):
    (env_root / "pipx_metadata.json").write_text("{}")
    assert cli._install_kind() == ("pipx", ["pipx", "upgrade", "compartment"])
    ran = []

    def run(cmd, *a, **k):
        ran.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(cli.subprocess, "run", run)
    from compartment import embed_daemon
    monkeypatch.setattr(embed_daemon, "stop", lambda *a, **k: {})
    cli.cmd_update(types.SimpleNamespace(source=True, no_app=True,
                                         vault="unused"))
    assert ran[0][:3] == ["pipx", "install", "--force"]
    assert ran[0][3].startswith("git+https://github.com/")


def test_a_plain_pip_install_is_still_upgraded_with_its_own_pip(env_root):
    kind, cmd = cli._install_kind()
    assert kind == "pip" and cmd[:3] == [sys.executable, "-m", "pip"]


def test_uninstall_names_the_pipx_command_for_a_pipx_install(env_root,
                                                            monkeypatch,
                                                            capsys):
    """`python -m pip uninstall` inside a pipx environment removes the
    package and leaves pipx listing a broken install whose launcher points
    at nothing."""
    (env_root / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(cli, "_tray_app",
                        lambda: (_ for _ in ()).throw(RuntimeError("no app")))
    monkeypatch.setattr(subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 1, "", ""))
    for name in ("unregister", "is_registered"):
        monkeypatch.setattr(cli.clients, name, lambda *a, **k: False)
    monkeypatch.setattr(cli.claude_desktop, "unregister", lambda: False)
    monkeypatch.setattr(cli.claude_hooks, "is_installed", lambda: False)
    monkeypatch.setattr(cli.agent_skill, "SKILL_TARGETS", {})
    monkeypatch.setattr(cli, "_remove_managed_claude_md", lambda: None)
    from compartment import menubar
    monkeypatch.setattr(menubar, "_is_generated", lambda path: False)
    monkeypatch.setenv("OPENCLAW_HOME", str(env_root / "openclaw"))
    monkeypatch.setenv("HERMES_HOME", str(env_root / "hermes"))
    cli.cmd_uninstall(types.SimpleNamespace(purge=False,
                                            vault=str(env_root / "v.vault")))
    out = capsys.readouterr().out
    assert out.rstrip().endswith("pipx uninstall compartment")
