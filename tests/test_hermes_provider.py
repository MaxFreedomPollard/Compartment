"""The Hermes memory provider, loaded from the plugin file the way Hermes
loads it, with a stand-in for Hermes's MemoryProvider base class: Hermes
itself is never imported or run here."""
import importlib.util
import json
import pathlib
import sys
import types

import pytest

from compartment import session
from compartment import vault as vault_module
from compartment.vault import Vault

PASS = "CorrectHorse"
PLUGIN = (pathlib.Path(__file__).resolve().parents[1] / "src" / "compartment"
          / "data" / "hermes-plugin" / "__init__.py")


@pytest.fixture()
def plugin(monkeypatch, vault_path):
    agent = types.ModuleType("agent")
    base = types.ModuleType("agent.memory_provider")
    base.MemoryProvider = object
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", base)
    spec = importlib.util.spec_from_file_location("hermes_compartment",
                                                  PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("COMPARTMENT_VAULT", vault_path)
    monkeypatch.setattr(vault_module, "keychain_get", lambda path: None)
    for name in ("COMPARTMENT_PASSPHRASE", "ENGRAM_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_CREDENTIAL_CHECK_SECONDS", 0, raising=False)
    return mod


def _search(provider):
    return json.loads(provider.handle_tool_call(
        "compartment_search", {"query": "which Postgres version runs"}))


def test_a_lock_reaches_the_hermes_provider(plugin, vault_path):
    """The provider has no auto-lock at all, and looked for a credential
    only when its handle was missing, locked or stale: after `compartment
    lock` it kept the key for the rest of the Hermes session."""
    v = Vault.create(vault_path, PASS, creator="test")
    session.store(vault_path, v._master)          # `compartment unlock`
    v.lock()
    provider = plugin.CompartmentMemoryProvider()
    try:
        provider.initialize("s1")
        assert "results" in _search(provider)
        session.clear(vault_path)                 # `compartment lock`
        out = _search(provider)
    finally:
        session.clear(vault_path)
    assert "compartment unlock" in out.get("message", ""), out
    assert provider._vault._locked and provider._vault._master is None


def test_the_provider_still_runs_on_an_engine_without_the_check(
        plugin, vault_path, monkeypatch):
    """plugin.yaml asks for compartment>=4.9.3, and session.watch_signature
    arrived in 4.11.0: on an older engine the provider opens and reads as
    it always did."""
    v = Vault.create(vault_path, PASS, creator="test")
    session.store(vault_path, v._master)
    v.lock()
    monkeypatch.delattr(session, "watch_signature")
    provider = plugin.CompartmentMemoryProvider()
    try:
        provider.initialize("s1")
        first, second = _search(provider), _search(provider)
    finally:
        session.clear(vault_path)
    assert "results" in first and "results" in second
