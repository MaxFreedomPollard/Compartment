"""The capture hook's command, as the shell Claude Code runs it in reads it.

Claude Code runs hook commands through bash on every platform: Git Bash on
Windows since 2.1.47. To bash an unquoted backslash is an escape character,
and $ and ` expand inside double quotes."""
import os
import subprocess
import sys

import pytest

from compartment import claude_hooks

WIN_EXE = r"C:\Users\ana\AppData\Roaming\Python\Python312\Scripts\compartment.exe"
WIN_VAULT = r"C:\Users\ana\.compartment\memory.vault"


def _bash_words(cmd):
    """The words bash makes of `cmd`, as bash itself splits them."""
    ran = subprocess.run(["bash", "-c", f'set -- {cmd}; for w in "$@"; do '
                          'printf "%s\\0" "$w"; done'],
                         capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
    return ran.stdout.split("\0")[:-1]


needs_bash = pytest.mark.skipif(sys.platform == "win32" or not any(
    os.access(os.path.join(d, "bash"), os.X_OK)
    for d in os.environ.get("PATH", "").split(os.pathsep)),
    reason="needs a bash to split the command")


@needs_bash
def test_a_windows_executable_path_reaches_git_bash_whole(monkeypatch):
    """Quoted only when it held a space, the usual Windows path reached
    Git Bash with its backslashes taken as escapes,
    C:UsersanaAppDataRoaming...compartment.exe, so the hook failed with
    "command not found" after every edit and captured nothing."""
    monkeypatch.setattr(claude_hooks, "_windows", lambda: True,
                        raising=False)
    cmd = claude_hooks.hook_command(WIN_EXE, WIN_VAULT)
    words = _bash_words(cmd)
    assert words[0].replace("/", "\\") == WIN_EXE
    assert words[1:3] == ["--vault", WIN_VAULT.replace("\\", "/")]
    assert words[3:] == ["hook", "capture"]
    assert claude_hooks.is_ours(cmd)


@needs_bash
@pytest.mark.parametrize("exe, vault", [
    ("/Applications/My Tools/compartment", "/v/m.vault"),
    ("/home/ana/$HOME/bin/compartment", "/home/ana/a `b` $c \"d\"/m.vault"),
    ("/opt/R&D (x86)/compartment", "/v/it's mine/m.vault"),
])
def test_a_path_with_shell_characters_in_it_is_one_word(exe, vault):
    cmd = claude_hooks.hook_command(exe, vault)
    assert _bash_words(cmd) == [exe, "--vault", vault, "hook", "capture"]
    assert claude_hooks.is_ours(cmd)


def test_a_windows_command_has_no_backslash_for_any_shell_to_eat(monkeypatch):
    monkeypatch.setattr(claude_hooks, "_windows", lambda: True,
                        raising=False)
    cmd = claude_hooks.hook_command(WIN_EXE, WIN_VAULT)
    assert "\\" not in cmd
    # A plain path stays bare, which PowerShell also runs as a command.
    assert cmd.startswith("C:/Users/ana/AppData/Roaming/Python/Python312/"
                          "Scripts/compartment.exe --vault ")
    spaced = claude_hooks.hook_command(
        r"C:\Program Files (x86)\Compartment\compartment.exe", WIN_VAULT)
    assert spaced.startswith('"C:/Program Files (x86)/Compartment/'
                             'compartment.exe" --vault ')


def test_the_forward_slash_vault_opens_the_same_session(monkeypatch):
    """The session store keys a vault by its normalised absolute path, so
    the forward-slash spelling the hook now writes on Windows is the same
    vault, unlocked by the same session, as the backslash one."""
    import ntpath
    from compartment import session
    with monkeypatch.context() as m:
        m.setattr(session.os, "path", ntpath)
        a = session._canon(WIN_VAULT)
        b = session._canon(WIN_VAULT.replace("\\", "/"))
    assert a == b


@pytest.mark.parametrize("settings", [
    '{"hooks": []}',
    '{"hooks": {"PostToolUse": {"a": 1}}}',
    '{"hooks": {"PostToolUse": ["x"]}}',
    '{"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": "x"}]}}',
])
def test_settings_whose_hooks_are_the_wrong_shape_are_refused_untouched(
        settings, tmp_path):
    """A list where the hooks object belongs, or a string where a matcher
    group does, was walked as if it were right and died with
    AttributeError, which `integrate claude`, `hook status` and the
    uninstaller do not catch: a traceback instead of the file's name and
    what is wrong with it. An event that was an object was replaced."""
    p = tmp_path / "settings.json"
    p.write_text(settings, encoding="utf-8")
    for call in (lambda: claude_hooks.install("compartment", settings=p),
                 lambda: claude_hooks.uninstall(settings=p),
                 lambda: claude_hooks.is_installed(settings=p)):
        with pytest.raises(ValueError, match="refusing to touch it"):
            call()
    assert p.read_text(encoding="utf-8") == settings


def test_settings_whose_hooks_are_null_get_the_hook(tmp_path):
    """`"hooks": null` is no hooks at all; install died with TypeError on
    it, which nothing catches."""
    p = tmp_path / "settings.json"
    p.write_text('{"hooks": null, "model": "opus"}', encoding="utf-8")
    claude_hooks.install("compartment", settings=p)
    assert claude_hooks.is_installed(settings=p) is True
    assert claude_hooks.uninstall(settings=p) is True
