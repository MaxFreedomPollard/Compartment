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
