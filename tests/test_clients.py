"""Writing into somebody else's configuration file is the dangerous part.

The tools that make Compartment reachable from Cursor or Zed or LM Studio are
not clever - they put one object into one JSON file. What makes them worth
testing is everything that must survive the edit: the other MCP servers the
user already had, the file when it will not parse, the entry when it is
already there. A memory server that eats somebody's Cursor config on the way
in has not integrated with anything.

Every case here starts from a config that already has another server in it,
because that is the state every real machine is in.
"""
import builtins
import contextlib
import errno
import io
import json
import os
import stat
import types

import pytest

from compartment import claude_desktop, claude_hooks, cli, clients


VAULT = "/v/memory.vault"

#: What a user's config looks like before we touch it. If any test finishes
#: with this missing or changed, the writer is not safe to ship.
OTHER = {"command": "other-server", "args": ["--flag"]}


def cfg_with_other(path, root="mcpServers"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({root: {"someone-elses": OTHER},
                                "unrelatedSetting": 42}, indent=2))
    return path


# ------------------------------------------------------------------ the table

def test_every_client_resolves_a_path_without_raising():
    for key, c in clients.CLIENTS.items():
        p = c.config_path()
        assert str(p), f"{key} produced an empty path"


def test_every_alias_points_at_a_real_client():
    for alias, key in clients.ALIASES.items():
        assert clients.resolve(alias) is clients.CLIENTS[key]


def test_aliases_and_casing_are_accepted():
    assert clients.resolve("Roo-Code").key == "roo"
    assert clients.resolve("  GEMINI-CLI ").key == "gemini"
    assert clients.resolve("nothing-like-this") is None


def test_the_clients_we_will_not_write_say_so():
    # Cherry Studio keeps MCP config in application state and Goose keeps it
    # in YAML we would reformat. Both must be declared, not attempted.
    assert clients.CLIENTS["cherry-studio"].writes is False
    assert clients.CLIENTS["goose"].writes is False
    assert clients.CLIENTS["cursor"].writes is True


# ------------------------------------------------------------- the JSON write

def test_registering_keeps_every_other_server(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is True
    data = json.loads(p.read_text())
    assert data["mcpServers"]["someone-elses"] == OTHER
    assert data["unrelatedSetting"] == 42
    assert data["mcpServers"]["compartment"]["args"][:2] == ["--vault", VAULT]


def test_the_backup_is_byte_exact(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    before = p.read_bytes()
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["backup"], "no backup was taken"
    from pathlib import Path
    assert Path(res["backup"]).read_bytes() == before


def test_registering_twice_updates_rather_than_duplicates(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    clients.register(clients.CLIENTS["cursor"], VAULT, path=p)
    clients.register(clients.CLIENTS["cursor"], "/other.vault", path=p)

    servers = json.loads(p.read_text())["mcpServers"]
    assert len(servers) == 2                    # theirs and exactly one ours
    assert servers["compartment"]["args"][1] == "/other.vault"


def test_a_config_that_will_not_parse_is_left_alone(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"mcpServers": {  // a comment JSON does not allow\n}')
    before = p.read_text()

    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is False
    assert "refusing to touch it" in res["reason"]
    assert res["snippet"] and "compartment" in res["snippet"]
    assert p.read_text() == before, "a config we could not parse was modified"


def test_a_missing_config_is_created(tmp_path):
    p = tmp_path / "nested" / "mcp.json"
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert res["written"] is True
    assert res["backup"] is None                # nothing existed to back up
    assert "compartment" in json.loads(p.read_text())["mcpServers"]


def test_a_root_key_that_is_not_an_object_is_refused(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": ["not", "an", "object"]}))
    res = clients.register(clients.CLIENTS["cursor"], VAULT, path=p)
    assert res["written"] is False
    assert json.loads(p.read_text())["mcpServers"] == ["not", "an", "object"]


# ------------------------------------------------------- the shapes that vary

def test_vscode_uses_servers_and_names_the_transport(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json", root="servers")
    clients.register(clients.CLIENTS["vscode"], VAULT, path=p)

    data = json.loads(p.read_text())
    assert "mcpServers" not in data
    assert data["servers"]["someone-elses"] == OTHER
    assert data["servers"]["compartment"]["type"] == "stdio"


def test_zed_uses_context_servers(tmp_path):
    p = cfg_with_other(tmp_path / "settings.json", root="context_servers")
    clients.register(clients.CLIENTS["zed"], VAULT, path=p)

    data = json.loads(p.read_text())
    assert data["context_servers"]["someone-elses"] == OTHER
    assert "compartment" in data["context_servers"]


def test_opencode_takes_one_command_array(tmp_path):
    p = cfg_with_other(tmp_path / "opencode.json", root="mcp")
    clients.register(clients.CLIENTS["opencode"], VAULT, path=p)

    entry = json.loads(p.read_text())["mcp"]["compartment"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert isinstance(entry["command"], list)
    assert entry["command"][1:3] == ["--vault", VAULT]


def test_each_client_is_told_which_caller_it_is(tmp_path):
    p = tmp_path / "mcp.json"
    clients.register(clients.CLIENTS["lmstudio"], VAULT, path=p)
    args = json.loads(p.read_text())["mcpServers"]["compartment"]["args"]
    assert args[args.index("--caller") + 1] == "lmstudio"


# --------------------------------------------------------------------- TOML

def test_codex_gets_a_table_appended_and_keeps_the_rest(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# a comment somebody wrote\nmodel = "o3"\n\n'
                 '[mcp_servers.other]\ncommand = "other"\n')

    res = clients.register(clients.CLIENTS["codex"], VAULT, path=p)
    text = p.read_text()

    assert res["written"] is True
    assert "# a comment somebody wrote" in text     # comments survive
    assert '[mcp_servers.other]' in text            # their server survives
    assert '[mcp_servers.compartment]' in text
    assert f'"{VAULT}"' in text


def test_codex_is_not_registered_twice(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[mcp_servers.compartment]\ncommand = "compartment"\n')
    res = clients.register(clients.CLIENTS["codex"], VAULT, path=p)

    assert res["written"] is False
    assert p.read_text().count("[mcp_servers.compartment]") == 1


def test_codex_unregister_takes_only_our_table(tmp_path):
    p = tmp_path / "config.toml"
    clients.register(clients.CLIENTS["codex"], VAULT, path=p)
    p.write_text(p.read_text() + '\n[mcp_servers.other]\ncommand = "x"\n')

    assert clients.unregister(clients.CLIENTS["codex"], path=p) is True
    text = p.read_text()
    assert "[mcp_servers.compartment]" not in text
    assert '[mcp_servers.other]' in text
    assert 'command = "x"' in text


# ----------------------------------------------------------------- removal

def test_unregister_removes_only_ours(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    clients.register(clients.CLIENTS["cursor"], VAULT, path=p)

    assert clients.unregister(clients.CLIENTS["cursor"], path=p) is True
    servers = json.loads(p.read_text())["mcpServers"]
    assert servers == {"someone-elses": OTHER}


def test_unregister_reports_when_we_were_never_there(tmp_path):
    p = cfg_with_other(tmp_path / "mcp.json")
    assert clients.unregister(clients.CLIENTS["cursor"], path=p) is False


# ------------------------------------------------------- the ones we only print

@pytest.mark.parametrize("key", [k for k, c in clients.CLIENTS.items()
                                 if not c.writes])
def test_a_manual_client_writes_nothing_and_hands_back_a_block(key, tmp_path):
    p = tmp_path / "would-be-config"
    res = clients.register(clients.CLIENTS[key], VAULT, path=p)

    assert res["written"] is False
    assert not p.exists(), f"{key} wrote a file it was told not to write"
    assert "compartment" in res["snippet"]


def test_the_printed_block_is_valid_json_for_json_clients():
    c = clients.CLIENTS["continue"]
    parsed = json.loads(clients.snippet(c, VAULT))
    assert "compartment" in parsed[c.root]


def test_omp_resolves_and_writes_agent_level_mcp_json():
    c = clients.CLIENTS["omp"]
    assert c.writes is True
    entry = json.loads(clients.snippet(c, VAULT))["mcpServers"]["compartment"]
    assert entry["args"][:2] == ["--vault", VAULT]
    assert entry["args"][-1] == "serve"


# -------------------------------------------------------------------- status

def test_present_is_false_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(clients.Path, "home",
                        classmethod(lambda cls: tmp_path))
    assert clients.CLIENTS["cursor"].present() is False


def test_status_has_a_row_for_every_client():
    rows = clients.status()
    assert len(rows) == len(clients.CLIENTS)
    assert {r["key"] for r in rows} == set(clients.CLIENTS)


# ---------------------------------------------------------- the command itself

def test_the_written_command_is_an_absolute_path(tmp_path, monkeypatch):
    """A GUI client launched from the Dock inherits launchd's PATH, and an
    install that is only Compartment.app never put `compartment` on any PATH
    for it to inherit. A bare name there names nothing."""
    import sys
    binf = tmp_path / "bin"
    binf.mkdir()
    (binf / "compartment").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(clients.shutil, "which", lambda n: None)
    assert clients.executable() == str(binf / "compartment")


def test_the_written_command_is_never_the_app_launcher(tmp_path, monkeypatch):
    """Inside Compartment.app the launcher sits in MacOS/ and is called
    `Compartment`; macOS filesystems are case-insensitive, so looking beside
    the interpreter for "compartment" finds it. A client pointed there would
    start the menu bar app instead of a memory server."""
    import sys
    macos = tmp_path / "Compartment.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "compartment").write_text("#!/bin/sh\n", encoding="utf-8")
    (macos / "python3").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(macos / "python3"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere"))
    monkeypatch.setattr(clients.shutil, "which",
                        lambda n: "/usr/local/bin/compartment")
    assert clients.executable() == "/usr/local/bin/compartment"


def test_the_command_falls_back_to_path_then_to_the_bare_name(tmp_path,
                                                              monkeypatch):
    import sys
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(clients.shutil, "which", lambda n: "/opt/bin/compartment")
    assert clients.executable() == "/opt/bin/compartment"
    monkeypatch.setattr(clients.shutil, "which", lambda n: None)
    assert clients.executable() == "compartment"


# ------------------------------------------ CLAUDE.md and OpenClaw's config

class _DiskFillsUp:
    """A text file that takes 4,096 characters and then reports the disk
    full, the way a write ends on a disk with no room left."""

    def __init__(self, f):
        self._f, self._room = f, 4096

    def write(self, s):
        if len(s) > self._room:
            self._f.write(s[:self._room])
            self._room = 0
            raise OSError(errno.ENOSPC, "No space left on device")
        self._room -= len(s)
        return self._f.write(s)

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


@contextlib.contextmanager
def _disk_full_in(directory, monkeypatch):
    """Every text file opened for writing in `directory` fills the disk after
    4,096 characters, whether the code opens it with open() or a Path
    method."""
    real_open, where = io.open, os.path.abspath(directory)

    def opening(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if ("w" in mode and "b" not in mode
                and isinstance(file, (str, os.PathLike))
                and os.path.dirname(os.path.abspath(file)) == where):
            return _DiskFillsUp(f)
        return f

    with monkeypatch.context() as m:
        m.setattr(io, "open", opening)
        m.setattr(builtins, "open", opening)
        yield


def _claude_md(tmp_path, monkeypatch):
    """40 KB of the user's own rules and last version's block."""
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_MD", str(md))
    rules = "".join(f"- my own rule {i}: explain the trade-off before "
                    "acting.\n" for i in range(640))
    md.write_text(rules + "\n" + cli._CLAUDE_MD_BEGIN + "\nlast version's "
                  "block\n" + cli._CLAUDE_MD_END + "\n", encoding="utf-8")
    return md, rules


def _symlink_or_skip(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"no symlinks here ({exc})")


def test_a_claude_md_rewrite_cut_short_leaves_it_whole_with_a_backup(
        tmp_path, monkeypatch):
    """The block was written back through a truncating open with no backup,
    so a write that stopped partway (a full disk, a kill) during `update`,
    `integrate claude` or init left the user's CLAUDE.md cut short: 40 KB of
    their own rules became 4 KB, and nothing kept a copy."""
    md, rules = _claude_md(tmp_path, monkeypatch)
    if os.name != "nt":
        md.chmod(0o600)
    before = md.read_bytes()

    with _disk_full_in(md.parent, monkeypatch):
        with pytest.raises(OSError):
            cli._write_managed_claude_md()
    assert md.read_bytes() == before                   # whole, not cut short
    backup = md.with_name(md.name + clients.BACKUP_SUFFIX)
    assert backup.read_bytes() == before
    assert sorted(p.name for p in md.parent.iterdir()) == sorted(
        [md.name, backup.name])                         # no temp file left

    # With room on the disk the block is replaced and nothing else moves.
    cli._write_managed_claude_md()
    text = md.read_text(encoding="utf-8")
    assert text.startswith(rules) and "last version's block" not in text
    assert text.count(cli._CLAUDE_MD_BEGIN) == 1
    assert backup.read_bytes() == before
    if os.name != "nt":                                 # a private file stays
        assert stat.S_IMODE(md.stat().st_mode) == 0o600  # private


def test_an_openclaw_write_cut_short_leaves_its_config_whole_with_a_backup(
        tmp_path, monkeypatch, capsys):
    """`integrate openclaw` took its backup and then wrote openclaw.json in
    place, through a truncating open, so a write cut short left the config,
    with every other server in it, cut short as well."""
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setenv("OPENCLAW_HOME", str(home))
    cfg = home / "openclaw.json"
    cfg.write_text(json.dumps({"mcpServers": {f"server-{i}": OTHER
                                              for i in range(100)}},
                              indent=2), encoding="utf-8")
    before = cfg.read_bytes()
    args = types.SimpleNamespace(target="openclaw", vault=VAULT)

    with _disk_full_in(home, monkeypatch):
        cli._integrate_target(args)
    assert "could not edit" in capsys.readouterr().out
    assert cfg.read_bytes() == before
    backup = cfg.with_name(cfg.name + clients.BACKUP_SUFFIX)
    assert backup.read_bytes() == before

    cli._integrate_target(args)
    servers = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
    assert "compartment" in servers and servers["server-99"] == OTHER
    assert backup.read_bytes() == before


@pytest.mark.parametrize("name", ["CLAUDE.md", "openclaw.json"])
def test_a_claude_md_or_openclaw_config_kept_in_dotfiles_stays_a_link(
        name, tmp_path, monkeypatch):
    """Both files were written in place, through the link. A replacement
    renamed into place has to go beside the file the link points at, or it
    turns the link into a plain copy that the dotfiles no longer reach."""
    dot = tmp_path / "dotfiles"
    dot.mkdir()
    real = dot / name
    if name == "CLAUDE.md":
        link = tmp_path / ".claude" / name
        monkeypatch.setenv("CLAUDE_MD", str(link))
        real.write_text("# kept in my dotfiles\n", encoding="utf-8")
        write = cli._write_managed_claude_md
    else:
        link = tmp_path / "openclaw-home" / name
        monkeypatch.setenv("OPENCLAW_HOME", str(link.parent))
        real.write_text(json.dumps({"mcpServers": {"theirs": OTHER}}),
                        encoding="utf-8")
        write = lambda: cli._integrate_target(  # noqa: E731
            types.SimpleNamespace(target="openclaw", vault=VAULT))
    before = real.read_bytes()
    _symlink_or_skip(link, real)

    write()
    assert link.is_symlink()
    assert os.path.samefile(link, real)
    text = real.read_text(encoding="utf-8")
    assert ("kept in my dotfiles" in text and cli._CLAUDE_MD_BEGIN in text
            if name == "CLAUDE.md"
            else set(json.loads(text)["mcpServers"]) == {"theirs",
                                                          "compartment"})
    backup = link.with_name(link.name + clients.BACKUP_SUFFIX)
    assert backup.read_bytes() == before
    assert os.listdir(dot) == [name]            # nothing left beside it


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root may write a read-only file")
def test_a_claude_md_the_user_made_read_only_is_left_alone(tmp_path,
                                                          monkeypatch):
    """Written in place, a read-only CLAUDE.md refused the write. Renaming a
    new copy over it would not ask, so the write has to."""
    md, _ = _claude_md(tmp_path, monkeypatch)
    before = md.read_bytes()
    md.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            cli._write_managed_claude_md()
        assert md.read_bytes() == before
        assert os.listdir(md.parent) == [md.name]   # no backup, no temp
    finally:
        md.chmod(0o644)


# ------------------------------------------- configs kept in dotfiles

#: Each writer and its remover, the file it writes, what is in that file
#: already, and a part of it that has to survive both.
_LINKED_WRITERS = {
    "cursor": (".cursor/mcp.json",
               '{"mcpServers": {"theirs": {"command": "x"}}}\n', '"theirs"',
               lambda p: clients.register(clients.CLIENTS["cursor"], VAULT,
                                          path=p),
               lambda p: clients.unregister(clients.CLIENTS["cursor"],
                                            path=p)),
    "codex": (".codex/config.toml", 'model = "o3"\n', 'model = "o3"',
              lambda p: clients.register(clients.CLIENTS["codex"], VAULT,
                                         path=p),
              lambda p: clients.unregister(clients.CLIENTS["codex"], path=p)),
    "claude-code-hook": (".claude/settings.json",
                         '{"permissions": {"allow": ["Bash"]}}\n',
                         '"permissions"',
                         lambda p: claude_hooks.install(
                             compartment_bin="compartment", vault=VAULT,
                             settings=p),
                         lambda p: claude_hooks.uninstall(settings=p)),
    "claude-desktop": ("Claude/claude_desktop_config.json",
                       '{"mcpServers": {"theirs": {"command": "x"}}}\n',
                       '"theirs"',
                       lambda p: claude_desktop.register(
                           "compartment", ["serve"], path=p),
                       lambda p: claude_desktop.unregister(path=p)),
}


@pytest.mark.parametrize("writer", sorted(_LINKED_WRITERS))
def test_every_writer_keeps_a_symlinked_config_a_link(writer, tmp_path):
    """os.replace renamed the new file over the link itself, so after a
    write ~/.cursor/mcp.json, Claude Code's settings.json or Codex's
    config.toml was a plain copy and the file in the user's dotfiles (a
    stow tree, a home-manager path) no longer reached the program. Init
    did this to ~/.claude/settings.json without being asked."""
    rel, content, theirs, add, remove = _LINKED_WRITERS[writer]
    dot = tmp_path / "dotfiles"
    dot.mkdir()
    real = dot / os.path.basename(rel)
    real.write_text(content, encoding="utf-8")
    link = tmp_path / "home" / rel
    _symlink_or_skip(link, real)

    add(link)
    assert link.is_symlink()
    assert os.path.samefile(link, real)
    text = real.read_text(encoding="utf-8")
    assert "compartment" in text and theirs in text
    backup = link.with_name(link.name + clients.BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == content

    remove(link)
    assert link.is_symlink()
    assert os.path.samefile(link, real)
    text = real.read_text(encoding="utf-8")
    assert "compartment" not in text and theirs in text
    assert os.listdir(dot) == [real.name]       # no temp file left there


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid")
                                        and os.geteuid() == 0),
                    reason="a directory's mode does not stop this user "
                           "writing in it")
def test_a_config_linked_into_a_directory_this_cannot_write_is_refused(
        tmp_path):
    """A link into a read-only tree (a Nix store path) cannot be written
    through. Renaming over the link "worked" by detaching it; now the
    clients get the block to paste and the other writers fail as they do
    for any file they cannot write, with the link and its file untouched."""
    store = tmp_path / "store"
    store.mkdir()
    home = tmp_path / "home"
    links = {}
    for rel in (".cursor/mcp.json", ".codex/config.toml",
                ".claude/settings.json", "Claude/claude_desktop_config.json"):
        real = store / os.path.basename(rel)
        real.write_text("", encoding="utf-8")
        links[rel] = home / rel
        _symlink_or_skip(links[rel], real)
    store.chmod(0o555)
    try:
        for rel in (".cursor/mcp.json", ".codex/config.toml"):
            client = clients.CLIENTS["cursor" if "cursor" in rel else "codex"]
            res = clients.register(client, VAULT, path=links[rel])
            assert res["written"] is False, rel
            assert "compartment" in res["snippet"]
            assert "cannot write" in res["reason"]
        with pytest.raises(OSError):
            claude_hooks.install(compartment_bin="compartment",
                                 settings=links[".claude/settings.json"])
        with pytest.raises(OSError):
            claude_desktop.register(
                "compartment", ["serve"],
                path=links["Claude/claude_desktop_config.json"])
        for rel, link in links.items():
            assert link.is_symlink(), rel
            assert link.read_text(encoding="utf-8") == ""
            assert os.listdir(link.parent) == [link.name]   # no backup
        assert sorted(os.listdir(store)) == sorted(
            os.path.basename(r) for r in links)             # no temp file
    finally:
        store.chmod(0o755)
