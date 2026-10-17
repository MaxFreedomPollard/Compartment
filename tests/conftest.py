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


def fill_clustered(vault, n, seed=11):
    """`n` organic records shaped like a vault people tag: groups of twenty
    near each other in embedding space, about a third of each group carrying
    the group's subject tag, nearly all of those and some others a broader
    area tag shared by ten groups, and one record in twenty a label of its
    own. Seeded random vectors stand in for embeddings, inserted through
    Store.insert and indexed once, so thousands take a fraction of a
    second."""
    import numpy as np
    rng = np.random.default_rng(seed)
    dim = int(vault.header.model["dim"])
    groups = max(1, n // 20)
    centres = rng.standard_normal((groups, dim)).astype(np.float32)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    for i in range(n):
        g = i % groups
        vec = centres[g] + rng.uniform(0.02, 0.045) * rng.standard_normal(
            dim).astype(np.float32)
        tags = []
        if rng.random() < 0.35:
            tags.append(f"topic-{g}")
            if rng.random() < 0.9:
                tags.append(f"area-{g % 10}")
        elif rng.random() < 0.3:
            tags.append(f"area-{g % 10}")
        if rng.random() < 0.05:
            tags.append(f"note-{i}")
        vault.db.insert(record_id=None, ns="main",
                        text=f"Synthetic note {i} in group {g}.",
                        vec=vec / np.linalg.norm(vec), tags=tags,
                        importance=0.5, quarantined=False, pack=None, prov={},
                        master_key=vault._master)
    vault._rebuild_index()


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

    The per-boot secret a credential is keyed with lives in the kernel, not
    under tmp_path, so it outlived the run until the next restart: about
    forty a run. The teardown destroys this store's, and that of the store
    the test pointed COMPARTMENT_SESSION_DIR at if it is under tmp_path.
    """
    store = tmp_path / "session-store"
    monkeypatch.setenv("COMPARTMENT_SESSION_DIR", str(store))
    yield
    forget_session_holder(store)
    # Plain strings, and best effort: a test that patched pathlib.Path or
    # sys.platform for its own purposes is still patched at this point.
    own = os.environ.get("COMPARTMENT_SESSION_DIR") or ""
    base = str(tmp_path).rstrip("/\\")
    if own and own != str(store) and own.startswith(base):
        forget_session_holder(own)


def forget_session_holder(session_dir) -> None:
    """Destroy the per-boot secret of the session store at `session_dir`, as
    a restart would. Its name is the one session._holder() gives that
    directory, so only that store's secret can go, and nothing is created on
    disk."""
    from compartment import session
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(session, "_session_dir",
                       lambda: pathlib.Path(session_dir))
            session._forget_boot_secret()
    except Exception:                                   # noqa: BLE001
        # Best effort: a test that stubbed winreg or sys.platform is still
        # patched at this point, and a holder that cannot be forgotten is a
        # kernel object that goes at the next restart, never a failure.
        pass


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


def assert_guards_in_force(tmp_path) -> None:
    """Fail unless the guards above still hold for this test.

    monkeypatch.undo() on the shared fixture undoes them along with the
    test's own patches, for the rest of the test and for its teardown: the
    session store goes back to whatever the outer environment names, which
    with nothing set is the real ~/.compartment/session, and PATH and SHELL
    go back to ones that reach the real agent tools. A test with patches of
    its own to undo keeps them on a MonkeyPatch of its own, and ends with
    this."""
    store = os.environ.get("COMPARTMENT_SESSION_DIR", "")
    assert store and pathlib.Path(store).is_relative_to(tmp_path), (
        f"COMPARTMENT_SESSION_DIR is {store!r}, not under this test's "
        f"tmp_path")
    own_bin = str(pathlib.Path(sys.executable).resolve().parent)
    path = os.environ.get("PATH", "").split(os.pathsep)
    assert path[0] == own_bin and (sys.platform == "win32" or set(path) <= {
        own_bin, "/usr/bin", "/bin", "/usr/sbin", "/sbin"}), (
        f"PATH is {os.environ.get('PATH')!r}, not cut down")
    assert os.environ.get("SHELL") == "/usr/bin/false", (
        f"SHELL is {os.environ.get('SHELL')!r}")
    for name in ("HERMES_HOME", "OPENCLAW_HOME"):
        assert pathlib.Path(os.environ.get(name, "/")).is_relative_to(
            tmp_path), f"{name} is {os.environ.get(name)!r}"
    assert _claude_desktop.config_path().is_relative_to(tmp_path)


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
