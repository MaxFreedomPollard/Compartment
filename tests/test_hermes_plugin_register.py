"""The Hermes provider plugin exposes the ``register(ctx)`` entry point.

Hermes discovers a user-installed memory provider by scanning its
``__init__.py`` for the ``MemoryProvider`` contract and loads it through
``register(ctx)`` first, falling back to the subclass scan. The Nous plugin
catalog's admission gate (``hermes plugins validate``) has no fallback: it
imports the plugin in a bare interpreter, calls ``register`` against a
recording context, and fails the entry with "no register() function" when
it is absent. Both shipped copies must carry it.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_INITS = (
    ROOT / "integrations" / "hermes" / "compartment" / "__init__.py",
    ROOT / "src" / "compartment" / "data" / "hermes-plugin" / "__init__.py",
)


class _RecordingContext:
    """The shape of Hermes's probe context: every registration is a no-op."""

    def __init__(self):
        self.memory_providers = []

    def register_memory_provider(self, provider):
        self.memory_providers.append(provider)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _load_plugin(path: pathlib.Path, monkeypatch):
    """Import the plugin by file path the way Hermes does, with the one
    Hermes module it imports (``agent.memory_provider``) stubbed out."""
    agent_pkg = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # the ABC the plugin subclasses
        pass

    memory_provider.MemoryProvider = MemoryProvider
    agent_pkg.memory_provider = memory_provider
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)

    name = f"compartment_hermes_plugin_{path.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, MemoryProvider


@pytest.mark.parametrize("path", PLUGIN_INITS, ids=lambda p: p.parent.name)
def test_register_hands_hermes_the_provider(path, monkeypatch):
    module, base = _load_plugin(path, monkeypatch)
    ctx = _RecordingContext()

    module.register(ctx)

    assert len(ctx.memory_providers) == 1
    provider = ctx.memory_providers[0]
    assert isinstance(provider, base)
    assert provider.name == "compartment"
    # The picker's "no setup needed" hint comes from an empty schema.
    assert provider.get_config_schema() == []


@pytest.mark.parametrize("path", PLUGIN_INITS, ids=lambda p: p.parent.name)
def test_register_never_imports_compartment_at_load(path, monkeypatch):
    """Importing the plugin and registering must not need the compartment
    package: Hermes enumerates providers before anything is installed, and
    the catalog probe runs in a bare interpreter."""
    monkeypatch.setitem(sys.modules, "compartment", None)  # import -> ImportError
    module, _ = _load_plugin(path, monkeypatch)
    module.register(_RecordingContext())
