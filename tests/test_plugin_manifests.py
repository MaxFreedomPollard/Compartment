"""Every store listing in the repository parses, says what its host needs,
and launches the same server.

Compartment is listed in a dozen places from one repository: the root is
at once a Claude Code plugin, a Codex plugin (through the Agent Plugins
manifest), an OpenClaw bundle, a Gemini CLI extension and a Hermes
package, and packaging/ holds the manifests other catalogues want. None of
those hosts runs in CI, so a manifest that drifts from its host's format,
or from the server it describes, fails on a user's machine and nowhere
else. These tests check each one against the parts of its host's current
format that matter (docs/PLUGINS.md names the source of each), and check
them against each other: one command, the same subcommand, a caller each
host can be told apart by, and no claim about the server that the server
does not make.

Offline, like the rest of the suite: nothing here fetches a schema.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


# ------------------------------------------------------------ MCP Registry --
#: The newest released server.json schema in modelcontextprotocol/registry
#: (docs/reference/server-json/CHANGELOG.md), still current in October 2026.
REGISTRY_SCHEMA = ("https://static.modelcontextprotocol.io/schemas/"
                   "2025-12-11/server.schema.json")


def test_server_json_targets_the_current_registry_schema():
    assert _json("server.json")["$schema"] == REGISTRY_SCHEMA


def test_server_json_name_is_the_one_pypi_proves_ownership_of():
    """The registry checks a PyPI package by finding `mcp-name: <name>` in
    its README, so the two must be the same string or every publish fails."""
    name = _json("server.json")["name"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {name}" in readme


def test_server_json_meets_the_registry_limits():
    d = _json("server.json")
    assert 1 <= len(d["description"]) <= 100        # schema maxLength
    assert d["repository"]["url"] == "https://github.com/MaxFreedomPollard/Compartment"
    pkg = d["packages"][0]
    assert pkg["registryType"] == "pypi"
    assert pkg["registryBaseUrl"] == "https://pypi.org"   # the only one allowed
    assert pkg["identifier"] == "compartment"
    assert pkg["transport"] == {"type": "stdio"}
    assert [a["value"] for a in pkg["packageArguments"]][-1] == "serve"


def test_server_json_gives_registry_clients_a_website_and_an_icon():
    """Clients that render the registry (VS Code's MCP gallery, GitHub's MCP
    registry, directory sites) show a blank tile and no link without these."""
    d = _json("server.json")
    assert d["websiteUrl"].startswith("https://")
    icon = d["icons"][0]
    assert icon["src"].startswith("https://") and len(icon["src"]) <= 255
    assert icon["mimeType"] == "image/png"
    assert all(re.fullmatch(r"\d+x\d+|any", s) for s in icon["sizes"])
    # the icon it names is the one in this repository, at the size it says
    local = ROOT / icon["src"].split("/main/", 1)[1]
    head = local.read_bytes()[:24]
    w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    assert icon["sizes"] == [f"{w}x{h}"]


def test_listing_pages_never_state_a_client_count_that_is_wrong():
    """`integrate --list` grows; a page that says "28 clients" is wrong the
    day it does. Pages in this set either say no number or the right one."""
    from compartment import clients
    for rel in ("registry.html", "llms-install.md", "docs/PLUGINS.md"):
        p = ROOT / rel
        if not p.exists():
            continue
        for n in re.findall(r"\b(\d+) (?:MCP )?clients\b", p.read_text(encoding="utf-8")):
            assert int(n) == len(clients.CLIENTS), (rel, n)
