"""The programs `integrate`, `update` and `uninstall` run: Hermes's CLI and
interpreter, pip, Claude Code's CLI, and the newly installed compartment.
Each is bounded and gets no stdin, so a program that hangs, or stops to ask
something, can neither stall an install nor read the user's keyboard.

Every program here is a fake on a private PATH under tmp_path, or a stub of
subprocess.run. The conftest guard keeps the real claude, hermes and
openclaw out of reach, and the fakes are found ahead of anything else."""
import contextlib
import os
import subprocess
import sys
import textwrap
import types

import pytest

from compartment import claude_desktop, claude_memory, cli

posix_fakes = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fakes are POSIX scripts, and the Hermes venv probed is the "
           "POSIX layout (bin/python)")


def _fake(path, code):
    """A program at `path` that runs `code` in this interpreter. exec keeps
    one process, so a timeout's kill ends it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    script = path.with_name(path.name + ".py")
    script.write_text(textwrap.dedent(code), encoding="utf-8")
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                    encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    """A private bin directory first on PATH, a log the fakes write to, an
    existing vault file and a Hermes home under tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir),
                                                os.environ["PATH"]]))
    log = tmp_path / "ran.log"
    vault = tmp_path / "memory.vault"
    vault.write_bytes(b"")              # integrate only checks that it exists
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return types.SimpleNamespace(bin=bin_dir, log=log, vault=str(vault),
                                 venv_python=hermes_home / "hermes-agent"
                                 / "venv" / "bin" / "python")


SLEEP = """
    import sys, time
    open({log!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
    time.sleep(10)
"""


def _integrate(target, vault, **extra):
    cli._integrate_target(types.SimpleNamespace(target=target, vault=vault,
                                                **extra))


@posix_fakes
def test_a_hermes_that_does_not_finish_leaves_the_command_to_run(
        tools, monkeypatch, capsys):
    """`hermes memory setup` ran with no timeout: init waited 154 s on it on
    one machine, 320 s on another while Hermes finished an update of its
    own, and the panel's Create button gave up after 900 s."""
    _fake(tools.bin / "hermes", SLEEP.format(log=str(tools.log)))
    monkeypatch.setattr(cli, "AGENT_TOOL_TIMEOUT", 1, raising=False)
    _integrate("hermes", tools.vault)
    out = capsys.readouterr().out
    assert "finish selection with:  hermes memory setup compartment" in out


@posix_fakes
def test_the_hermes_venv_import_probe_is_bounded(tools, monkeypatch, capsys):
    """The `import compartment` probe in Hermes's own interpreter had no
    timeout at all. One that does not answer gets the command to run by
    hand, and pip is not started in a venv that is not answering."""
    _fake(tools.venv_python, SLEEP.format(log=str(tools.log)))
    monkeypatch.setattr(cli, "HERMES_IMPORT_TIMEOUT", 1, raising=False)
    _integrate("hermes", tools.vault)
    out = capsys.readouterr().out
    assert f"{tools.venv_python} -m pip install compartment" in out
    assert "-m pip" not in tools.log.read_text(encoding="utf-8")


@posix_fakes
def test_a_pip_install_into_the_hermes_venv_is_bounded(tools, monkeypatch,
                                                       capsys):
    _fake(tools.venv_python, f"""
        import sys, time
        open({str(tools.log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
        if sys.argv[1:3] == ["-c", "import compartment"]:
            sys.exit(1)
        time.sleep(10)
    """)
    monkeypatch.setattr(cli, "HERMES_PIP_TIMEOUT", 1, raising=False)
    _integrate("hermes", tools.vault)
    out = capsys.readouterr().out
    assert f"{tools.venv_python} -m pip install compartment" in out


@contextlib.contextmanager
def _keyboard(lines):
    """This process's stdin, at the descriptor every child inherits, is a
    pipe holding `lines`, as a terminal would hold what the user types."""
    r, w = os.pipe()
    os.write(w, "".join(f"{x}\n" for x in lines).encode())
    os.close(w)
    saved = os.dup(0)
    os.dup2(r, 0)
    os.close(r)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)


READS = """
    import sys
    line = sys.stdin.readline()
    open({log!r}, "a").write(repr(line) + "\\n")
    if sys.argv[1:3] == ["-c", "import compartment"]:
        sys.exit(1)
    print("Added stdio MCP server compartment")
"""


@posix_fakes
def test_no_program_integrate_runs_reads_the_keyboard(tools, tmp_path,
                                                      monkeypatch, capsys):
    """Each one inherited the terminal's stdin, so a prompt in pip, Hermes or
    Claude Code read the user's keys, and behind captured output nobody
    could even see that it was waiting."""
    for exe in (tools.venv_python, tools.bin / "hermes", tools.bin / "claude"):
        _fake(exe, READS.format(log=str(tools.log)))
    monkeypatch.setattr(claude_desktop, "present", lambda: False)
    monkeypatch.setattr(claude_memory, "DEFAULT_ROOT", tmp_path / "none")
    monkeypatch.setenv("CLAUDE_MD", str(tmp_path / "CLAUDE.md"))
    with _keyboard(["typed at the keyboard"] * 8):
        _integrate("hermes", tools.vault)
        _integrate("claude", tools.vault, no_import=True, no_hooks=True)
    reads = tools.log.read_text(encoding="utf-8").splitlines()
    assert len(reads) == 4, reads   # import probe, pip, hermes, claude
    assert set(reads) == {"''"}, reads


def _recording(monkeypatch):
    ran = []

    def run(argv, **kwargs):
        ran.append((list(argv), kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    return ran


def test_update_gives_the_new_build_no_stdin(monkeypatch):
    """update's version probe and its `integrate --refresh` inherited the
    terminal, with their output captured."""
    from compartment import embed_daemon
    monkeypatch.setattr(embed_daemon, "stop", lambda: {})
    ran = _recording(monkeypatch)
    cli.cmd_update(types.SimpleNamespace(source=False, no_app=True,
                                         vault="unused"))
    probes = [kw for argv, kw in ran
              if argv[1:] in (["--version"], ["integrate", "--refresh"])]
    assert len(probes) == 2, ran
    assert all(kw.get("stdin") is subprocess.DEVNULL for kw in probes), ran


def test_uninstall_gives_claude_no_stdin(monkeypatch, tmp_path):
    from compartment import agent_skill, claude_hooks, menubar
    monkeypatch.setattr(cli, "_tray_app",
                        lambda: (_ for _ in ()).throw(RuntimeError("none")))
    monkeypatch.setattr(menubar, "_is_generated", lambda path: False)
    monkeypatch.setattr(claude_hooks, "is_installed", lambda: False)
    monkeypatch.setattr(agent_skill, "remove", lambda target: False)
    monkeypatch.setattr(agent_skill, "is_installed", lambda target: False)
    # Uninstall also takes compartment out of every client's config and of
    # CLAUDE.md, all found from the home directory, which is tmp_path here.
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("CLAUDE_MD", str(tmp_path / ".claude" / "CLAUDE.md"))
    ran = _recording(monkeypatch)
    cli.cmd_uninstall(types.SimpleNamespace(purge=False,
                                            vault=str(tmp_path / "v")))
    removes = [kw for argv, kw in ran if argv[:3] == ["claude", "mcp",
                                                      "remove"]]
    assert len(removes) == 2, ran
    assert all(kw.get("stdin") is subprocess.DEVNULL for kw in removes), ran
