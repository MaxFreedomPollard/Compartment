"""Every version field in the tree moves together.

RELEASING.md lists the files that carry a version and warns that nothing
downstream fails when one of them drifts. That warning was accurate and it
was not enough: the Hermes provider plugin's `plugin.yaml` sat at 1.7.0
while the package shipped 4.9.2, and `hermes plugins list` printed 1.7.0 in
its Version column for everyone who installed it. A checklist a human reads
is not a gate; this is.
"""
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The provider plugin ships inside the wheel, so the copy under src/ is what
#: `compartment integrate hermes` actually installs. The one under
#: integrations/ is the readable source of it. They must not disagree.
PLUGIN_YAMLS = (
    ROOT / "integrations" / "hermes" / "compartment" / "plugin.yaml",
    ROOT / "src" / "compartment" / "data" / "hermes-plugin" / "plugin.yaml",
)


def _version() -> str:
    ns: dict = {}
    exec((ROOT / "src" / "compartment" / "__init__.py").read_text(
        encoding="utf-8").split("from .")[0], ns)
    return ns["__version__"]


def _semver(v: str) -> str:
    """The three-part form. `4.9.2` is already one; `2.1` becomes `2.1.0`.

    The registry orders releases by semver and treats a plain `2.1` as
    unorderable, which is why server.json carries both spellings.
    """
    parts = v.split(".")
    return ".".join(parts + ["0"] * (3 - len(parts)))


def _yaml_version(p: pathlib.Path) -> str:
    m = re.search(r"^version:\s*['\"]?([0-9][^'\"\s]*)", p.read_text(encoding="utf-8"),
                  re.M)
    assert m, f"{p} has no version line"
    return m.group(1)


def test_pyproject_matches_the_package():
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == _version()


def test_server_json_carries_both_spellings():
    """packages[0].version is the string PyPI serves; the top-level version is
    the semver form the registry orders by. Setting both to the plain form is
    what published an entry that never became latest."""
    d = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert d["packages"][0]["version"] == _version()
    assert d["version"] == _semver(_version())


def test_gemini_extension_json_matches_the_package():
    """The Gemini CLI extensions gallery shows this version and the CLI
    compares it on update, so it moves with the package like plugin.json."""
    d = json.loads((ROOT / "gemini-extension.json").read_text(encoding="utf-8"))
    assert d["version"] == _semver(_version())


def test_plugin_json_matches_the_package():
    d = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert d["version"] == _semver(_version())


def test_claude_code_plugin_json_matches_the_package():
    """Claude Code decides whether an installed plugin is out of date by this
    field, and a plugin that declares none is pinned to whatever commit it
    was installed from, so it has to exist and has to move with every
    release. Codex and OpenClaw read the same file when they install from
    this marketplace."""
    d = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(
        encoding="utf-8"))
    assert d["version"] == _semver(_version())


def test_marketplace_entries_that_carry_a_version_match_the_package():
    """The entry may repeat the plugin's version; Claude Code shows the
    entry's metadata before plugin.json's, so a stale copy there would be
    the number users see. None is set today; this stops one from rotting."""
    d = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(
        encoding="utf-8"))
    for entry in d["plugins"]:
        if "version" in entry:
            assert entry["version"] == _semver(_version()), entry["name"]


# ----------------------------------------------------------- coverage ------
#: Every file above, and nothing else, may carry the package's version.
GATED = {
    "pyproject.toml",
    "server.json",
    "plugin.json",
    "gemini-extension.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "integrations/hermes/compartment/plugin.yaml",
    "src/compartment/data/hermes-plugin/plugin.yaml",
}

#: Files whose version field is their own, not the package's.
OWN_VERSION = {
    # The tombstone for the pre-rename PyPI name. It only depends on
    # compartment; republishing it under every Compartment version would
    # upload an identical empty package each release.
    "packaging/engram-memory-vault/pyproject.toml",
    # The skill's own revision, which Hermes shows; it changes when the
    # skill's text does, not when the package does.
    "skills/compartmentalize/SKILL.md",
    "src/compartment/data/agent-skill/SKILL.md",
}

#: Known drift in a file this test does not own the fix for. Strict, so the
#: day it is fixed the marker has to go and the file joins GATED.
KNOWN_STALE = {
    # The plugin.yaml prepared for a PR to hermes-agent still says 1.7.0 and
    # compartment>=2.3. It is not shipped by anything in this repository,
    # which is why nothing caught it.
    "integrations/hermes/upstream/plugin.yaml",
}

_SKIP_DIRS = {".git", "tests", "models", "build", "dist", ".venv", "venv",
              "node_modules", "__pycache__"}


def _carries_a_version(p: pathlib.Path) -> bool:
    """A top-level version field, in whatever syntax the file is written in,
    or a pin on the compartment package itself."""
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".json":
        try:
            d = json.loads(text)
        except ValueError:
            return False
        if not isinstance(d, dict):
            return False
        return "version" in d or any(
            isinstance(e, dict) and "version" in e
            for e in d.get("plugins", []) or [])
    if p.name == "SKILL.md":
        head = text.split("---", 2)[1] if text.startswith("---") else ""
        return bool(re.search(r"^\s*version:", head, re.M))
    if p.suffix in (".yaml", ".yml", ".toml"):
        return bool(re.search(r"^version\s*[:=]", text, re.M)
                    or re.search(r"\bcompartment\s*==\s*[0-9]", text))
    return False


def _manifests_with_a_version() -> set[str]:
    found = set()
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if any(part in _SKIP_DIRS or part.endswith(".egg-info")
               for part in rel.parts[:-1]):
            continue
        if not p.is_file():
            continue
        if p.suffix in (".json", ".yaml", ".yml", ".toml") or p.name == "SKILL.md":
            if _carries_a_version(p):
                found.add(rel.as_posix())
    return found


def test_every_file_that_carries_a_version_is_gated():
    """A new manifest nobody added to this file is exactly how plugin.yaml
    drifted. Anything in the tree with a version field must be checked
    above, or say here why its version is its own."""
    unaccounted = _manifests_with_a_version() - GATED - OWN_VERSION - KNOWN_STALE
    assert not unaccounted, (
        f"{sorted(unaccounted)} carry a version this test does not check: "
        "gate it against the package version above, or add it to "
        "OWN_VERSION with the reason it differs")


@pytest.mark.parametrize("rel", sorted(KNOWN_STALE))
@pytest.mark.xfail(strict=True, reason="stale copy in a file this test does "
                   "not own; see KNOWN_STALE")
def test_known_stale_copies_match_the_package(rel):
    assert _yaml_version(ROOT / rel) == _version()


@pytest.mark.parametrize("path", PLUGIN_YAMLS, ids=lambda p: p.parent.name)
def test_hermes_plugin_yaml_matches_the_package(path):
    """`hermes plugins list` prints this in its Version column, so a stale
    number here tells the user they are running something they are not."""
    assert _yaml_version(path) == _version()


def test_the_shipped_plugin_is_the_one_in_integrations():
    """The wheel's copy is what gets installed; a divergence would ship
    something no one has read."""
    a, b = (p.read_text(encoding="utf-8") for p in PLUGIN_YAMLS)
    assert a == b
