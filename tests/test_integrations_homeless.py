"""A container run as an arbitrary uid has no HOME and no passwd entry, so
Path.home() raises there. Importing a module must not ask for it: every
`compartment` command imports these, whether or not it touches Claude Code
or a skills directory."""
import os
import subprocess
import sys

import pytest

MODULES = ["clients", "claude_desktop", "claude_hooks", "claude_memory",
           "agent_skill", "wire"]

PRELUDE = """
import pathlib, sys
try:
    import pwd
except ImportError:                      # Windows has no passwd database
    pass
else:
    def _no_entry(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")
    pwd.getpwuid = _no_entry
try:
    pathlib.Path.home()
except RuntimeError:
    pass
else:
    sys.exit("the home directory could still be found")
"""


def _homeless_env():
    env = {k: v for k, v in os.environ.items()
           if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                        "CLAUDE_CONFIG_DIR", "HERMES_HOME", "OPENCLAW_HOME",
                        "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH")}
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.abspath(src), env.get("PYTHONPATH", "")])
    return env


@pytest.mark.parametrize("module", MODULES)
def test_a_module_imports_where_there_is_no_home_directory(module):
    """claude_hooks, claude_memory and agent_skill each called Path.home()
    at import, so in such a container every command died before it ran."""
    code = PRELUDE + f"import compartment.{module}\n"
    ran = subprocess.run([sys.executable, "-c", code], env=_homeless_env(),
                         capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr[-2000:]


def test_the_import_root_has_a_value_to_show_with_no_home():
    """`import-claude --help` shows DEFAULT_ROOT; with no home it names a
    path that holds nothing, and discovery finds nothing in it."""
    code = PRELUDE + (
        "from compartment import claude_memory\n"
        "print(claude_memory.DEFAULT_ROOT)\n"
        "assert claude_memory.discover() == []\n")
    ran = subprocess.run([sys.executable, "-c", code], env=_homeless_env(),
                         capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr[-2000:]
    assert ".claude" in ran.stdout


def test_the_env_overrides_still_win_when_asked_for(monkeypatch, tmp_path):
    from compartment import agent_skill, claude_hooks, claude_memory
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "o"))
    for module, name in ((claude_memory, "DEFAULT_ROOT"),
                         (claude_hooks, "SETTINGS")):
        if name in vars(module):
            monkeypatch.delattr(module, name)
    assert claude_hooks.SETTINGS == tmp_path / "c" / "settings.json"
    assert claude_hooks.settings_path() == tmp_path / "c" / "settings.json"
    assert claude_memory.DEFAULT_ROOT == tmp_path / "c" / "projects"
    assert agent_skill.agent_home("claude") == tmp_path / "c"
    assert agent_skill.agent_home("hermes") == tmp_path / "h"
    assert agent_skill.agent_home("openclaw") == tmp_path / "o"
