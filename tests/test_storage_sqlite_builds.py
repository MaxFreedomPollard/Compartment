"""Compartment on a Python whose SQLite was built without FTS5.

Python compiled against an older enterprise distribution's SQLite (CentOS
and RHEL 7, Amazon Linux 2) and some minimal builds have no FTS5. There is no
degraded mode to offer - the full-text table is part of the vault image - so
the requirement is a refusal that names the interpreter and the fix, on the
first command anyone runs, which is `compartment init`.
"""
import sqlite3

import pytest

from compartment import store as store_mod
from compartment.crypto import CryptoError
from compartment.store import Store, StoreError
from compartment.vault import Vault

from conftest import PASS


class _NoFts5:
    """A connection to a SQLite with no fts5 module: everything else works,
    and anything that needs the module fails the way that SQLite fails."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    @staticmethod
    def _needs_fts5(sql: str) -> bool:
        low = sql.lower()
        return "fts5" in low or "from fts" in low or "into fts" in low

    def execute(self, sql, *args):
        if self._needs_fts5(sql):
            raise sqlite3.OperationalError("no such module: fts5")
        return self._conn.execute(sql, *args)

    def executescript(self, sql):
        if self._needs_fts5(sql):
            raise sqlite3.OperationalError("no such module: fts5")
        return self._conn.executescript(sql)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def _store_connects_through(monkeypatch, proxy) -> None:
    """The store's sqlite3, handing out connections wrapped in `proxy`."""
    real = sqlite3.connect

    class _Module:
        Row = sqlite3.Row
        OperationalError = sqlite3.OperationalError
        sqlite_version = sqlite3.sqlite_version

        @staticmethod
        def connect(*args, **kwargs):
            return proxy(real(*args, **kwargs))

    monkeypatch.setattr(store_mod, "sqlite3", _Module)


@pytest.fixture()
def sqlite_without_fts5(monkeypatch):
    _store_connects_through(monkeypatch, _NoFts5)


def test_a_new_vault_is_refused_with_the_fix_not_a_bare_sqlite_error(
        sqlite_without_fts5):
    """The friendly refusal sat after the schema was created, and creating
    the schema is where such a SQLite first fails, so a new vault died on
    "no such module: fts5" and the message was only ever reached when an
    existing vault was opened."""
    with pytest.raises(StoreError) as caught:
        Store()
    msg = str(caught.value)
    assert "FTS5" in msg and sqlite3.sqlite_version in msg
    assert "python.org" in msg, "it says where a working Python comes from"


def test_init_reports_it_as_a_vault_error(sqlite_without_fts5, vault_path):
    """What `compartment init` and the panel's Create button catch."""
    with pytest.raises(CryptoError, match="FTS5"):
        Vault.create(vault_path, PASS, creator="t")


def test_an_existing_vault_is_refused_the_same_way(monkeypatch):
    image = Store().serialize()
    _store_connects_through(monkeypatch, _NoFts5)
    with pytest.raises(StoreError, match="FTS5"):
        Store(image)


def test_other_schema_errors_are_not_called_a_missing_fts5(monkeypatch):
    """Only the missing module earns that message; anything else is still
    the error it was."""
    class _Broken(_NoFts5):
        def executescript(self, sql):
            raise sqlite3.OperationalError("database is locked")

    _store_connects_through(monkeypatch, _Broken)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        Store()
