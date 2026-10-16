"""Deterministic capture, and the full `compartment integrate claude` path.

These run on every OS in CI, which is the point: the install flow used to be
verified by hand on one machine, so nothing caught a platform-specific break.
"""
import io
import json
import os
import shutil
import subprocess
import types

import pytest

from compartment import claude_hooks, claude_memory, cli, clients

PASS = "CorrectHorse"

MEM = """---
name: deploy-key
description: Production deploy key lives in 1Password
metadata:
  type: reference
---

The production deploy key is in 1Password under "prod-deploy".
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated Claude Code home: memory files, settings, CLAUDE.md."""
    memdir = tmp_path / ".claude" / "projects" / "-proj" / "memory"
    memdir.mkdir(parents=True)
    (memdir / "deploy-key.md").write_text(MEM, encoding="utf-8")
    (memdir / "MEMORY.md").write_text("- [k](deploy-key.md) - hook\n",
                                      encoding="utf-8")
    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(claude_memory, "DEFAULT_ROOT",
                        tmp_path / ".claude" / "projects")
    monkeypatch.setattr(claude_hooks, "SETTINGS", settings)
    monkeypatch.setenv("CLAUDE_MD", str(tmp_path / ".claude" / "CLAUDE.md"))
    return types.SimpleNamespace(root=tmp_path, memdir=memdir,
                                 settings=settings,
                                 memfile=memdir / "deploy-key.md")


# ----------------------------------------------------------- hook install

def test_install_creates_settings_with_our_hook(home):
    out = claude_hooks.install(compartment_bin="compartment")
    data = json.loads(home.settings.read_text(encoding="utf-8"))
    group = data["hooks"][claude_hooks.EVENT][0]
    assert group["matcher"] == claude_hooks.MATCHER
    cmd = group["hooks"][0]
    assert cmd["type"] == "command" and claude_hooks.MARKER in cmd["command"]
    assert isinstance(cmd["timeout"], int)      # docs: seconds
    assert out["backup"] is None                # nothing existed to back up


def test_install_preserves_other_peoples_hooks(home):
    existing = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "mine.sh"}]}],
        claude_hooks.EVENT: [{"matcher": "Bash",
                              "hooks": [{"type": "command",
                                         "command": "someone-else.sh"}]}]}}
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text(json.dumps(existing), encoding="utf-8")
    claude_hooks.install(compartment_bin="compartment")
    data = json.loads(home.settings.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "mine.sh"
    cmds = [h["command"] for g in data["hooks"][claude_hooks.EVENT]
            for h in g["hooks"]]
    assert "someone-else.sh" in cmds
    assert any(claude_hooks.MARKER in c for c in cmds)


def test_install_is_idempotent(home):
    claude_hooks.install(compartment_bin="compartment")
    claude_hooks.install(compartment_bin="compartment")
    data = json.loads(home.settings.read_text(encoding="utf-8"))
    ours = [h for g in data["hooks"][claude_hooks.EVENT] for h in g["hooks"]
            if claude_hooks.MARKER in h["command"]]
    assert len(ours) == 1                       # refreshed, not duplicated


def test_install_is_idempotent_with_a_pinned_vault(home):
    """Regression: with --vault pinned the command reads
    `compartment --vault ... hook capture`, so an ownership check looking for one
    literal substring stopped matching and every re-install added a duplicate.
    `integrate claude` pins the vault, so this is the DEFAULT path."""
    claude_hooks.install(compartment_bin="compartment", vault="/some/where.vault")
    claude_hooks.install(compartment_bin="compartment", vault="/some/where.vault")
    data = json.loads(home.settings.read_text(encoding="utf-8"))
    ours = [h for g in data["hooks"][claude_hooks.EVENT] for h in g["hooks"]
            if claude_hooks.is_ours(h["command"])]
    assert len(ours) == 1
    assert "--vault" in ours[0]["command"]
    assert claude_hooks.is_installed() is True
    assert claude_hooks.uninstall() is True      # and it can be removed again


def test_install_backs_up_existing_settings(home):
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text('{"env": {"A": "1"}}', encoding="utf-8")
    out = claude_hooks.install(compartment_bin="compartment")
    assert out["backup"] and json.loads(
        open(out["backup"], encoding="utf-8").read())["env"] == {"A": "1"}
    assert json.loads(home.settings.read_text(encoding="utf-8"))["env"] == {"A": "1"}


def test_install_refuses_malformed_settings(home):
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        claude_hooks.install(compartment_bin="compartment")


@pytest.mark.parametrize("raw", ["null", "[]"])
def test_install_refuses_settings_that_are_json_but_not_an_object(home, raw):
    """`null` or `[]` parsed, and .setdefault then failed on it with an
    AttributeError no caller expects. Malformed settings are refused with
    ValueError, and so are these, untouched."""
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        claude_hooks.install(compartment_bin="compartment")
    assert home.settings.read_text(encoding="utf-8") == raw


def test_uninstall_removes_only_ours(home):
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text(json.dumps({"hooks": {claude_hooks.EVENT: [
        {"matcher": "Bash", "hooks": [{"type": "command",
                                       "command": "theirs.sh"}]}]}}),
        encoding="utf-8")
    claude_hooks.install(compartment_bin="compartment")
    assert claude_hooks.is_installed() is True
    assert claude_hooks.uninstall() is True
    assert claude_hooks.is_installed() is False
    cmds = [h["command"] for g in json.loads(
        home.settings.read_text(encoding="utf-8"))["hooks"][claude_hooks.EVENT]
        for h in g["hooks"]]
    assert cmds == ["theirs.sh"]


def test_uninstall_when_absent_is_false(home):
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text("{}", encoding="utf-8")
    assert claude_hooks.uninstall() is False


def _installed_command(settings):
    groups = json.loads(settings.read_text(encoding="utf-8"))["hooks"][
        claude_hooks.EVENT]
    return [h["command"] for g in groups for h in g["hooks"]
            if claude_hooks.is_ours(h["command"])][0]


def test_the_capture_hook_names_a_compartment_that_exists(
        home, vault_path, monkeypatch, tmp_path):
    """The hook was written as a bare `compartment ... hook capture`
    whenever `compartment` was not on the PATH of the process installing
    it (the .app install puts it on none; an unactivated venv; a login
    item), while the MCP registration from the same run named the resolved
    executable. Claude Code's shell then answered "command not found" after
    every Write and Edit of any file, and nothing was ever captured."""
    nothing = tmp_path / "empty-bin"
    nothing.mkdir()
    monkeypatch.setenv("PATH", str(nothing))
    assert shutil.which("compartment") is None
    exe = clients.executable()
    assert os.path.isabs(exe)

    cli._install_capture_hook(vault_path)          # init, integrate claude
    cmd = _installed_command(home.settings)
    assert cmd == claude_hooks.hook_command(exe, vault_path)
    assert claude_hooks.hook_command(vault=vault_path) == cmd   # hook install
    assert f'--vault "{vault_path}"' in cmd

    if os.name != "nt":                 # run it the way Claude Code does
        edit = json.dumps({"tool_name": "Edit",
                           "tool_input": {"file_path": str(tmp_path / "a.py")}})
        ran = subprocess.run(["/bin/sh", "-c", cmd], input=edit, text=True,
                             capture_output=True, timeout=60)
        assert ran.returncode == 0, ran.stderr

    # A resolved path with a space in it is still quoted for the shell.
    monkeypatch.setattr(clients, "executable",
                        lambda: "/Applications/My Tools/compartment")
    assert claude_hooks.hook_command(vault="/v/m.vault") == (
        '"/Applications/My Tools/compartment" --vault "/v/m.vault" '
        "hook capture")


# ------------------------------------------------------------ file filter

def test_is_memory_file_accepts_only_claude_memory_notes(tmp_path):
    ok = tmp_path / ".claude" / "projects" / "p" / "memory" / "a.md"
    assert claude_hooks.is_memory_file(ok)
    # the index is a table of contents, not a fact
    assert not claude_hooks.is_memory_file(ok.parent / "MEMORY.md")
    # ordinary project files must never be swept into the vault
    assert not claude_hooks.is_memory_file(tmp_path / "src" / "main.py")
    assert not claude_hooks.is_memory_file(tmp_path / "notes" / "todo.md")
    assert not claude_hooks.is_memory_file(ok.with_suffix(".txt"))


# --------------------------------------------------------------- capture

def _payload(path):
    """Exactly the shape Claude Code documents for PostToolUse."""
    return json.dumps({"session_id": "s", "cwd": ".",
                       "hook_event_name": "PostToolUse", "tool_name": "Write",
                       "tool_input": {"file_path": str(path)},
                       "tool_output": {}})


def test_capture_stores_a_memory_write(home, vault_path, monkeypatch):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    res = claude_hooks.capture(io.StringIO(_payload(home.memfile)),
                               vault_path=vault_path)
    assert res["stored"] is True
    v2 = Vault.unlock(vault_path, passphrase=PASS)
    assert v2.status()["organic_records"] == 1
    hit = v2.search("where is the deploy key", caller="t")["results"]
    assert any("1Password" in h["text"] for h in hit)


def test_capture_ignores_non_memory_writes(home, vault_path, tmp_path):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    other = tmp_path / "app.py"
    other.write_text("print('hi')", encoding="utf-8")
    res = claude_hooks.capture(io.StringIO(_payload(other)),
                               vault_path=vault_path)
    assert res["stored"] is False and res["reason"] == "not a memory file"
    assert Vault.unlock(vault_path, passphrase=PASS).status()["organic_records"] == 0


@pytest.mark.parametrize("raw", ["", "   ", "not json", "{}", '{"tool_input":{}}'])
def test_capture_never_raises_on_bad_payloads(raw, vault_path):
    res = claude_hooks.capture(io.StringIO(raw), vault_path=vault_path)
    assert res["stored"] is False and "reason" in res


def test_capture_on_missing_file_is_quiet(home, vault_path):
    res = claude_hooks.capture(
        io.StringIO(_payload(home.memdir / "gone.md")), vault_path=vault_path)
    assert res["stored"] is False


def test_capture_with_locked_vault_does_not_break_the_edit(home, vault_path):
    """A locked vault is the user's choice - the hook must stay silent."""
    from compartment.vault import Vault
    from compartment import session
    Vault.create(vault_path, PASS, creator="t").lock()
    session.clear(vault_path)
    res = claude_hooks.capture(io.StringIO(_payload(home.memfile)),
                               vault_path=vault_path)
    assert res == {"stored": False, "reason": "vault locked"}


def test_capture_is_idempotent_on_repeated_writes(home, vault_path):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    first = claude_hooks.capture(io.StringIO(_payload(home.memfile)),
                                 vault_path=vault_path)
    second = claude_hooks.capture(io.StringIO(_payload(home.memfile)),
                                  vault_path=vault_path)
    assert first["stored"] is True and second["duplicate"] is True
    assert Vault.unlock(vault_path, passphrase=PASS).status()["organic_records"] == 1


# ------------------------------------- the whole `integrate claude` path
# Previously verified only by hand, on one machine. Now every OS in CI.

def _integrate(home, vault_path, monkeypatch, no_import=False, no_hooks=False):
    """Run cmd_integrate with the `claude` CLI stubbed, capturing its argv."""
    import shutil as _sh
    import subprocess as _sp
    calls = []

    def fake_which(name):
        return {"claude": "/usr/local/bin/claude",
                "compartment": "/usr/local/bin/compartment"}.get(name)

    real_run = _sp.run

    def fake_run(argv, **kw):
        # Intercept ONLY the claude CLI. The vault's boot-session credential
        # shells out too, and swallowing that breaks unlocking.
        if not (argv and "claude" in str(argv[0])):
            return real_run(argv, **kw)
        calls.append(argv)
        return types.SimpleNamespace(stdout="Added stdio MCP server compartment",
                                     stderr="", returncode=0)

    monkeypatch.setattr(_sh, "which", fake_which)
    monkeypatch.setattr(_sp, "run", fake_run)
    args = types.SimpleNamespace(target="claude", vault=vault_path,
                                 no_import=no_import, no_hooks=no_hooks)
    cli.cmd_integrate(args)
    return calls


def test_integrate_claude_end_to_end(home, vault_path, monkeypatch, capsys):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    (home.root / ".claude" / "CLAUDE.md").write_text("MY OWN NOTES\n",
                                                     encoding="utf-8")

    calls = _integrate(home, vault_path, monkeypatch)

    # 1. registered the MCP server with the right argv
    assert calls, "claude mcp add was never invoked"
    argv = calls[0]
    assert argv[1:4] == ["mcp", "add", "--scope"]
    assert "compartment" in argv and "serve" in argv and vault_path in argv

    # 2. CLAUDE.md: our block added, their notes untouched
    md = (home.root / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "MY OWN NOTES" in md and "REPLACES any other memory" in md

    # 3. existing file memories imported
    assert Vault.unlock(vault_path, passphrase=PASS).status()["organic_records"] == 1

    # 4. capture hook installed
    assert claude_hooks.is_installed() is True


def test_integrate_is_idempotent(home, vault_path, monkeypatch):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    _integrate(home, vault_path, monkeypatch)
    _integrate(home, vault_path, monkeypatch)
    assert Vault.unlock(vault_path, passphrase=PASS).status()["organic_records"] == 1
    data = json.loads(home.settings.read_text(encoding="utf-8"))
    ours = [h for g in data["hooks"][claude_hooks.EVENT] for h in g["hooks"]
            if claude_hooks.MARKER in h["command"]]
    assert len(ours) == 1
    md = (home.root / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert md.count("REPLACES any other memory") == 1


def test_integrate_opt_outs_are_honoured(home, vault_path, monkeypatch):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    _integrate(home, vault_path, monkeypatch, no_import=True, no_hooks=True)
    assert Vault.unlock(vault_path, passphrase=PASS).status()["organic_records"] == 0
    assert claude_hooks.is_installed() is False


def test_integrate_survives_a_missing_claude_cli(home, vault_path, monkeypatch,
                                                 capsys):
    """No `claude` on PATH must print manual instructions, not crash."""
    import shutil as _sh
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    monkeypatch.setattr(_sh, "which", lambda n: None if n == "claude" else "compartment")
    cli.cmd_integrate(types.SimpleNamespace(
        target="claude", vault=vault_path, no_import=False, no_hooks=False))
    out = capsys.readouterr().out
    assert "claude mcp add" in out              # told the user how to do it
    assert claude_hooks.is_installed() is True  # everything else still ran


def test_integrate_reports_malformed_settings_without_dying(home, vault_path,
                                                            monkeypatch, capsys):
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    home.settings.parent.mkdir(parents=True, exist_ok=True)
    home.settings.write_text("{broken", encoding="utf-8")
    _integrate(home, vault_path, monkeypatch)
    out = capsys.readouterr().out
    assert "not valid JSON" in out and "compartment hook install" in out
    # the import still happened - one broken file cannot stop the rest
    assert Vault.unlock(vault_path, passphrase=PASS).status()["organic_records"] == 1


# --- parser-level regression cover -----------------------------------------
# The tests above build an argparse Namespace by hand, so they kept passing
# while `integrate claude` crashed for every real user: the code read
# `args.no_hooks` and the parser never defined `--no-hooks`. These go through
# the real parser, which is the only way that class of bug shows up.

def _parsed(monkeypatch, handler_name, argv):
    """Run argv through the real parser, capturing args instead of dispatching."""
    seen = {}
    monkeypatch.setattr(cli, handler_name, lambda args: seen.update(vars(args)))
    cli.main(argv)
    return seen


def test_integrate_claude_accepts_the_documented_no_hooks_flag(monkeypatch):
    seen = _parsed(monkeypatch, "cmd_integrate",
                   ["integrate", "claude", "--no-hooks"])
    assert seen["target"] == "claude"
    assert seen["no_hooks"] is True


def test_integrate_claude_defaults_no_hooks_to_false(monkeypatch):
    seen = _parsed(monkeypatch, "cmd_integrate", ["integrate", "claude"])
    # the read that used to raise AttributeError
    assert seen["no_hooks"] is False
    assert seen["no_import"] is False


def test_bare_hook_has_a_handler_instead_of_crashing(monkeypatch):
    seen = _parsed(monkeypatch, "cmd_hook", ["hook"])
    assert seen["hook_cmd"] is None


def test_integrate_claude_goes_on_past_a_claude_md_that_is_not_utf8(
        home, vault_path, monkeypatch, capsys):
    """One byte that is not UTF-8 in the user's own CLAUDE.md raised
    UnicodeDecodeError, which the OSError handler did not catch: `integrate
    claude` died after registering the servers, before importing the
    existing memories and before the capture hook, and init reported
    "could not connect Claude Code" for a registration that had happened."""
    from compartment.vault import Vault
    from compartment import session
    v = Vault.create(vault_path, PASS, creator="t")
    session.store(vault_path, v._master)
    v.lock()
    md = home.root / ".claude" / "CLAUDE.md"
    md.write_bytes("Prefer the caf\xe9 wifi.\n".encode("cp1252"))
    before = md.read_bytes()

    _integrate(home, vault_path, monkeypatch)
    out = capsys.readouterr().out
    assert "could not update CLAUDE.md: not UTF-8" in out
    assert md.read_bytes() == before                  # never rewritten
    assert Vault.unlock(vault_path, passphrase=PASS).status()[
        "organic_records"] == 1                        # the import ran
    assert claude_hooks.is_installed() is True         # and the hook
