import contextlib
import os
import pathlib
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Set at import, not only in the autouse fixture below: pytest builds
# session-scoped fixtures such as seeded_vault_path BEFORE a test's
# function-scoped fixtures, so their Vault.create ran in the raw process
# environment and started a shared embedding daemon on the developer's real
# socket path. The fixture still exists so a test can flip it with
# monkeypatch and have it restored.
os.environ.setdefault("COMPARTMENT_EMBED_DAEMON", "0")

from compartment import claude_desktop as _claude_desktop  # noqa: E402
from compartment.vault import Vault  # noqa: E402

PASS = "CorrectHorse"

#: Captured before anything patches it, for the handful of tests that are
#: about where Claude Desktop keeps its config rather than about writing it.
_REAL_CLAUDE_DESKTOP_CONFIG_PATH = _claude_desktop.config_path


@pytest.fixture()
def real_claude_desktop_config_path():
    """`claude_desktop.config_path` as it ships, past the isolation below."""
    return _REAL_CLAUDE_DESKTOP_CONFIG_PATH


def seed_pack_bytes() -> bytes:
    return (SRC / "compartment" / "data" / "starter.mpack").read_bytes()


@contextlib.contextmanager
def statements_run(conn):
    """Every SQL statement `conn` executes inside the block, as SQLite traces
    it: with its parameters filled in, and fts5's own internal statements
    marked with a leading "--"."""
    ran = []
    conn.set_trace_callback(ran.append)
    try:
        yield ran
    finally:
        conn.set_trace_callback(None)


def table_walks(conn, statements) -> list[str]:
    """The statements SQLite plans as a walk over every row of the records
    table or the full-text table, from its own EXPLAIN QUERY PLAN. A walk of
    a plain table is a SCAN; fts5 narrows a scan only by rowid or MATCH, so
    one with no usable constraint shows nothing after the colon."""
    walks = []
    for sql in statements:
        if not sql.lstrip().upper().startswith(("SELECT", "DELETE", "UPDATE")):
            continue
        for row in conn.execute("EXPLAIN QUERY PLAN " + sql):
            detail = row[3]
            if (re.match(r"SCAN (TABLE )?records\b", detail) or re.fullmatch(
                    r"SCAN (TABLE )?fts VIRTUAL TABLE INDEX \d+:", detail)):
                walks.append(f"{sql}  [{detail}]")
    return walks


@pytest.fixture()
def vault_path(tmp_path):
    return str(tmp_path / "test.vault")


@pytest.fixture(autouse=True)
def no_real_session_store(monkeypatch, tmp_path):
    """No test writes a credential into the machine's real session store.

    Any test that unlocks or creates a vault stores a boot-bound credential,
    and the session directory defaults to the real ~/.compartment/session -
    so a full run quietly left hundreds of dead .session files there, on the
    machine of whoever ran it. That is exactly what the fixture below says a
    suite run must never do. Tests that are about the store already pick
    their own directory, and a setenv inside a test wins over this one.
    """
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(tmp_path / "session-store"))


@pytest.fixture(autouse=True)
def no_shared_embedding_daemon(monkeypatch):
    """Every test embeds in its own process unless it asks for the daemon.

    The shared daemon is the right default for a machine full of agents and
    the wrong one for a test suite: the first vault a test created would
    start a daemon in the developer's real session directory, every later
    test would talk to it, and it would sit there holding a model for five
    minutes after the run. The tests that are about the daemon set this
    variable back to 1 themselves, with a socket in a temp directory, and
    stop what they started.
    """
    monkeypatch.setenv("COMPARTMENT_EMBED_DAEMON", "0")


@pytest.fixture(autouse=True)
def no_real_supervisors(monkeypatch, tmp_path, request):
    """No test talks to the machine's own launchd, systemd or Task Scheduler,
    or writes a start-at-login entry into the real home directory.

    Running the suite must not change the computer it runs on. Without this,
    a Linux box with a user manager would have `set_login` tests enable a
    real user service on it and a Windows box would gain a real logon task,
    installed by whichever test happened to run and removed by nobody - and
    the autostart tests, which patch only sys.platform, would write into the
    real ~/.config on any machine at all.

    launchd is the same fault with a worse ending, and it was live until
    this: a fake HOME moves the plist but not the domain, because
    `_gui_domain` asks the real uid and LAUNCH_AGENT_LABEL is a module
    constant. So a macOS run of tests/test_status_bar_install.py ran
    `launchctl bootout gui/<real uid>/<real label>` and left the developer's
    own login item deregistered with its plist still on disk, then
    bootstrapped a job under the same label whose RunAtLoad started a second
    menu bar app.

    Tests that are about those code paths override this with their own fake
    or their own XDG_CONFIG_HOME, which is what monkeypatch ordering gives
    them for free. The one test that is about the live launchd asks for it by
    name with the real_launchctl marker, and is deselected by default.
    """
    from compartment import claude_desktop, menubar, systray
    # Claude Desktop keeps its MCP servers in a real file in the user's real
    # Application Support directory, and `integrate claude` writes it. The
    # Claude Code half of that path has a fixture faking its settings file;
    # this half never had one, so a suite run pointed the developer's own
    # Claude Desktop at a pytest temp vault, deleted the vault moments later,
    # and left the config naming it. Silent, and it outlives the run: the
    # damage is only visible the next time somebody opens Claude Desktop and
    # finds it has no memory.
    monkeypatch.setattr(claude_desktop, "config_path",
                        lambda: tmp_path / "claude-desktop"
                        / claude_desktop.FILENAME)
    if request.node.get_closest_marker("real_launchctl") is None:
        monkeypatch.setattr(menubar, "_launchctl",
                            lambda *a: (1, "launchctl: the test suite does "
                                           "not talk to the real launchd"))
    monkeypatch.setattr(systray, "_systemctl",
                        lambda *a: (1, "Failed to connect to bus: No such "
                                       "file or directory"))
    monkeypatch.setattr(systray, "_schtasks",
                        lambda *a: (1, "ERROR: The system cannot find the "
                                       "file specified."))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def no_real_agent_tools(monkeypatch, tmp_path):
    """No test, and no `compartment` child a test starts, can reach the real
    `claude`, `hermes` or `openclaw` installed on the machine running the
    suite.

    `init` wires every agent it finds, and it finds them through user_path(),
    which asks the login shell for PATH and then appends this process's own
    PATH and ~/.local/bin. A scratch HOME is therefore no protection at all:
    the end-to-end test that drives the real `compartment init` ran the real
    `hermes memory setup` on a developer's machine, under the scratch HOME.
    Hermes installed a second runtime there and rewrote its own launcher, an
    absolute path inside the real ~/.hermes, to point at it; when pytest
    removed the scratch directory, Hermes no longer started.

    So the login shell becomes one that fails, which leaves the shell lookup
    empty, and PATH is cut down to the interpreter's own bin directory plus
    the system directories, so `compartment` still resolves and nothing the
    user installed does. The floor user_path() adds (~/.local/bin and the
    rest) is under the scratch HOME, which holds no tools. The agents' home
    directories are pointed into the test's own tmp_path as well. A test
    about user_path() itself sets SHELL and PATH on top of this.
    """
    from compartment import menubar
    monkeypatch.setenv("SHELL", "/usr/bin/false")
    own_bin = str(pathlib.Path(sys.executable).resolve().parent)
    if sys.platform == "win32":
        profile = os.environ.get("USERPROFILE", "")
        keep = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                if p and not (profile and p.lower().startswith(profile.lower()))]
        monkeypatch.setenv("PATH", os.pathsep.join([own_bin, *keep]))
    else:
        monkeypatch.setenv("PATH", os.pathsep.join(
            [own_bin, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "agents" / "hermes"))
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "agents" / "openclaw"))
    monkeypatch.setattr(menubar, "_USER_PATH", None)   # the per-process cache


@pytest.fixture()
def vault(vault_path):
    v = Vault.create(vault_path, PASS, creator="test")
    yield v


@pytest.fixture(scope="session")
def seeded_vault_path(tmp_path_factory):
    from compartment import packs
    p = str(tmp_path_factory.mktemp("seeded") / "seeded.vault")
    v = Vault.create(p, PASS, creator="test")
    packs.seed_records(v, seed_pack_bytes(), caller="test")
    v.lock()
    return p


@pytest.fixture()
def seeded_vault(seeded_vault_path):
    return Vault.unlock(seeded_vault_path, passphrase=PASS)
