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


# ------------------------------------------- Agent Plugins (Hermes, Codex) --
#: Agent Plugins 1.0.0 is still the published release (1.1.0 is a working
#: draft that Codex, Hermes and OpenClaw do not accept yet), and both of its
#: schemas are closed: a host must reject the plugin on any field outside
#: these, so this is the whole vocabulary.
AGENT_PLUGIN_FIELDS = {"$schema", "name", "version", "description", "author",
                       "homepage", "repository", "license", "keywords",
                       "extensions"}
AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def test_the_agent_plugins_manifest_stays_inside_the_closed_schema():
    d = _json("plugin.json")
    assert set(d) <= AGENT_PLUGIN_FIELDS, set(d) - AGENT_PLUGIN_FIELDS
    assert d["$schema"] == AGENT_PLUGIN_SCHEMA
    assert re.fullmatch(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
                        d["name"]) and len(d["name"]) <= 64
    assert set(d["author"]) <= {"name", "email", "url"}
    assert all(isinstance(v, dict) for v in d.get("extensions", {}).values())


def test_the_agent_plugins_mcp_json_stays_inside_the_closed_schema():
    d = _json("mcp.json")
    assert set(d) == {"$schema", "mcpServers"}
    assert d["$schema"] == AGENT_MCP_SCHEMA
    for server in d["mcpServers"].values():
        assert set(server) <= {"type", "command", "args", "env", "cwd"}
        assert server["type"] == "stdio"
        # one executable token, never a path or a shell line: Codex, Hermes
        # and OpenClaw all refuse anything else for this format
        assert re.fullmatch(r"[A-Za-z0-9._-]+", server["command"])
        cwd = server.get("cwd", "./")
        assert re.match(r"(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))", cwd)


def test_codex_gets_a_listing_of_its_own_from_the_agent_plugins_manifest():
    """Codex installs from .claude-plugin/marketplace.json but loads the root
    plugin.json first when it declares the Agent Plugins schema, and without
    a com.openai extension it lists the plugin as "compartment", category
    Other, with no prompt or logo. The extension is where Codex looks."""
    ui = _json("plugin.json")["extensions"]["com.openai"]["interface"]
    for field in ("displayName", "shortDescription", "longDescription",
                  "developerName", "category", "capabilities", "defaultPrompt"):
        assert ui[field], field
    assert 1 <= len(ui["defaultPrompt"]) <= 3
    assert all(len(p) <= 128 for p in ui["defaultPrompt"])
    for field in ("websiteURL", "privacyPolicyURL"):
        assert ui[field].startswith("https://")
    assert ui["logo"].startswith("./") and (ROOT / ui["logo"]).is_file()


# ------------------------------------------------------ one-click pages -----
def _deeplinks(rel: str) -> list[str]:
    """The meta refresh and the fallback anchor, which must be one link."""
    import html
    s = (ROOT / rel).read_text(encoding="utf-8")
    refresh = re.search(r'http-equiv="refresh" content="0; url=([^"]+)"', s).group(1)
    anchors = re.findall(r'<a href="((?!https?:)[a-z]+://[^"]+)"', s)
    return [html.unescape(refresh)] + [html.unescape(a) for a in anchors]


def _caller(args: list[str]) -> str:
    return args[args.index("--caller") + 1]


def test_the_lm_studio_button_registers_what_integrate_registers():
    """The caller names the client in the audit log and is the key an ACL
    entry is written against; `integrate lmstudio` writes `lmstudio`, so a
    vault configured for that caller must see the same name from the
    button. The manual block on the page is the same entry."""
    import base64
    from urllib.parse import parse_qs, urlsplit
    from compartment import clients
    links = _deeplinks("add/lmstudio.html")
    assert len(set(links)) == 1, links
    q = parse_qs(urlsplit(links[0]).query)
    assert q["name"] == ["compartment"]
    entry = json.loads(base64.b64decode(q["config"][0]))
    assert entry["command"] == "compartment" and entry["args"][-1] == "serve"
    assert _caller(entry["args"]) == clients.CLIENTS["lmstudio"].key
    page = (ROOT / "add/lmstudio.html").read_text(encoding="utf-8")
    manual = json.loads(re.search(r"<pre>(.*?)</pre>", page, re.S).group(1))
    assert manual["mcpServers"]["compartment"] == entry


def test_the_goose_button_uses_a_command_goose_accepts():
    """goose refuses a deeplink whose command is not one of its launchers
    (cu, docker, jbang, npx, uvx, goose), so the bare `compartment` the
    other manifests use would be rejected; uvx runs the PyPI package."""
    from urllib.parse import parse_qs, urlsplit
    from compartment import clients
    links = _deeplinks("add/goose.html")
    assert len(set(links)) == 1, links
    q = parse_qs(urlsplit(links[0]).query)
    assert q["cmd"] == ["uvx"] and q["name"] == ["Compartment"]
    assert q["arg"][0] == "compartment" and q["arg"][-1] == "serve"
    assert _caller(q["arg"]) == clients.CLIENTS["goose"].key
    page = (ROOT / "add/goose.html").read_text(encoding="utf-8")
    assert f"<pre>uvx {' '.join(q['arg'])}</pre>" in page


# --------------------------------------------- Claude Desktop (MCP Bundle) --
def _registered_tools() -> list[str]:
    import asyncio
    from compartment import server
    return [t.name for t in asyncio.run(server.mcp.list_tools())]


def _build_mcpb():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_mcpb", ROOT / "tools" / "build_mcpb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_bundle_is_a_uv_bundle_claude_desktop_can_run():
    """manifest_version 0.4 is the MCPB release that added the uv server
    type, the only type that can carry compiled dependencies (onnxruntime,
    PyNaCl, argon2-cffi, numpy) to every platform from one bundle."""
    m = _json("packaging/mcpb/manifest.json")
    assert m["manifest_version"] == "0.4"
    assert m["name"] == "compartment" and m["author"]["name"]
    server = m["server"]
    assert server["type"] == "uv"
    assert (ROOT / "packaging/mcpb" / server["entry_point"]).is_file()
    cfg = server["mcp_config"]
    assert cfg["command"] == "uv"
    assert cfg["args"][:3] == ["run", "--directory", "${__dirname}"]
    assert cfg["args"][-1] == server["entry_point"]


def test_the_bundle_lists_exactly_the_tools_the_server_registers():
    """Claude Desktop shows this list before the server ever runs."""
    m = _json("packaging/mcpb/manifest.json")
    assert [t["name"] for t in m["tools"]] == _registered_tools()
    assert m["tools_generated"] is False


def test_the_bundle_starts_the_server_as_claude_desktop():
    """The same caller `integrate claude` writes into Claude Desktop's own
    config, so the two routes are one client to the audit log and ACL."""
    entry = (ROOT / "packaging/mcpb/server/main.py").read_text(encoding="utf-8")
    assert 'main(["--caller", "claude-desktop", "serve"])' in entry
    assert '"--caller", "claude-desktop", "serve"' in (
        ROOT / "src/compartment/cli.py").read_text(encoding="utf-8")


def test_the_bundle_pins_an_interpreter_its_dependencies_have_wheels_for():
    """uv otherwise picks the newest Python it can get, and onnxruntime
    trails Python releases: a bundle without a pin, installed the week a
    new Python ships, cannot build its environment."""
    pinned = (ROOT / "packaging/mcpb/.python-version").read_text().strip()
    text = (ROOT / "packaging/mcpb/pyproject.toml").read_text(encoding="utf-8")
    lo, hi = re.search(r'requires-python = ">=(\d+\.\d+),<(\d+\.\d+)"', text).groups()
    as_tuple = lambda v: tuple(int(x) for x in v.split("."))   # noqa: E731
    assert as_tuple(lo) <= as_tuple(pinned) < as_tuple(hi)


def test_the_bundle_build_is_complete_and_reproducible(tmp_path):
    import zipfile
    mod = _build_mcpb()
    a = mod.build(tmp_path / "a", with_lock=False)
    b = mod.build(tmp_path / "b", with_lock=False)
    assert a.name == f"compartment-{mod.package_version()}.mcpb"
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
    assert names == set(mod.CONTENTS)
    assert manifest == _json("packaging/mcpb/manifest.json")
    assert {manifest["icon"], manifest["server"]["entry_point"]} <= names


def test_the_bundle_lock_failure_says_what_to_do(tmp_path, monkeypatch):
    """The lock pins the release, so it only resolves once the release is on
    PyPI; a build run too early must say so, not end in a traceback."""
    import subprocess
    import pytest
    mod = _build_mcpb()
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="--no-lock"):
        mod.build(tmp_path, with_lock=True)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(SystemExit, match="after the PyPI publish"):
        mod.build(tmp_path, with_lock=True)
    assert not list(tmp_path.glob("*.mcpb*"))


def test_the_bundle_build_refuses_a_bundle_for_another_release():
    import pytest
    mod = _build_mcpb()
    with pytest.raises(SystemExit, match="not the package's"):
        mod.check("0.0.1")


# ---------------------------------------------------- Docker MCP Catalog ----
def _yaml(rel: str):
    import pytest
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def _dockerfile_entrypoint() -> list[str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    return json.loads(re.search(r"^ENTRYPOINT (\[.*\])$", text, re.M).group(1))


def test_the_docker_catalog_entry_builds_from_this_repository():
    """docker/mcp-registry builds mcp/compartment from the Dockerfile at a
    pinned commit; the entry is useless if either is wrong."""
    d = _yaml("packaging/docker-mcp/server.yaml")
    assert d["name"] == "compartment" and d["type"] == "server"
    assert d["image"] == "mcp/compartment"
    assert d["source"]["project"] == "https://github.com/MaxFreedomPollard/Compartment"
    assert re.fullmatch(r"[0-9a-f]{40}", d["source"]["commit"])
    assert d["about"]["title"] and d["about"]["icon"].startswith("https://")
    assert d["meta"]["category"]


def test_the_docker_catalog_runs_the_container_offline_on_the_users_vault():
    """No network at all, enforced by Docker; the user's vault folder mounted
    where the image's entrypoint looks for it; the passphrase from Docker
    Desktop's secret store, as the Dockerfile documents."""
    d = _yaml("packaging/docker-mcp/server.yaml")
    assert d["run"]["disableNetwork"] is True
    entry = _dockerfile_entrypoint()
    vault = entry[entry.index("--vault") + 1]
    (volume,) = d["run"]["volumes"]
    param, target = re.fullmatch(r"\{\{compartment\.(\w+)\}\}:(/\S+)", volume).groups()
    assert pathlib.PurePosixPath(vault).parent == pathlib.PurePosixPath(target)
    assert param in d["config"]["parameters"]["properties"]
    assert param in d["config"]["parameters"]["required"]
    assert [s["env"] for s in d["config"]["secrets"]] == ["COMPARTMENT_PASSPHRASE"]
    assert all(s["name"].startswith("compartment.") for s in d["config"]["secrets"])
    assert entry[-1] == "serve"
    assert not re.search(r"^EXPOSE\b", (ROOT / "Dockerfile").read_text(
        encoding="utf-8"), re.M)


# -------------------------------------------------------------- Continue ----
def test_the_continue_block_is_a_block_continue_loads():
    from compartment import clients
    d = _yaml("packaging/continue/compartment.yaml")
    assert d["schema"] == "v1" and d["name"] and d["version"]
    (server,) = d["mcpServers"]
    assert server["type"] == "stdio" and server["command"] == "compartment"
    assert server["args"][-1] == "serve"
    assert _caller(server["args"]) == clients.CLIENTS["continue"].key


# ------------------------------------------------- across every listing -----
def _listings() -> dict[str, tuple[str, list[str]]]:
    """host -> (command, compartment arguments) for every place this
    repository tells a host how to start the server."""
    import base64
    from urllib.parse import parse_qs, urlsplit
    out = {}
    out["claude-code"] = tuple(_json(".claude-plugin/plugin.json")["mcpServers"]
                               ["compartment"][k] for k in ("command", "args"))
    out["agent-plugins"] = tuple(_json("mcp.json")["mcpServers"]["compartment"][k]
                                 for k in ("command", "args"))
    out["gemini"] = tuple(_json("gemini-extension.json")["mcpServers"]
                          ["compartment"][k] for k in ("command", "args"))
    q = parse_qs(urlsplit(_deeplinks("add/lmstudio.html")[0]).query)
    lm = json.loads(base64.b64decode(q["config"][0]))
    out["lmstudio"] = (lm["command"], lm["args"])
    q = parse_qs(urlsplit(_deeplinks("add/goose.html")[0]).query)
    out["goose"] = (q["arg"][0], q["arg"][1:])          # uvx <package> <args>
    entry = _dockerfile_entrypoint()
    out["docker"] = (entry[0], entry[1:])
    main_py = (ROOT / "packaging/mcpb/server/main.py").read_text(encoding="utf-8")
    out["claude-desktop"] = ("compartment", json.loads(
        re.search(r"main\((\[.*?\])\)", main_py).group(1)))
    try:
        cont = _yaml("packaging/continue/compartment.yaml")["mcpServers"][0]
        out["continue"] = (cont["command"], cont["args"])
    except Exception:                                   # noqa: BLE001
        pass                                            # no PyYAML here
    return out


def test_every_listing_starts_the_same_server():
    """One executable, global options only before the subcommand, and the
    subcommand last: `compartment [--vault V] [--caller C] serve`."""
    for host, (command, args) in _listings().items():
        assert command == "compartment", host
        assert args[-1] == "serve", host
        opts = args[:-1]
        assert len(opts) % 2 == 0, (host, args)
        assert set(opts[::2]) <= {"--vault", "--caller"}, (host, args)


def test_every_listing_names_its_host_as_integrate_does():
    """The caller is the audit log's name for a client and the key ACL
    entries are written against. Where `compartment integrate` wires the
    same host, a listing must use the caller integrate writes, or one app
    shows up as two clients; and no two hosts may share a caller."""
    from compartment import clients
    listings = _listings()
    callers = {host: _caller(args) for host, (_, args) in listings.items()}
    assert len(set(callers.values())) == len(callers), callers
    for host, caller in callers.items():
        if host in clients.CLIENTS:
            assert caller == clients.CLIENTS[host].key, (host, caller)
    assert callers["claude-code"] == "claude-code"      # integrate claude
    assert callers["claude-desktop"] == "claude-desktop"


def test_no_listing_claims_a_tool_count_the_server_does_not_have():
    count = len(_registered_tools())
    for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
                "plugin.json", "gemini-extension.json", "server.json",
                "packaging/mcpb/manifest.json", "packaging/docker-mcp/server.yaml",
                "packaging/continue/compartment.yaml", "llms-install.md",
                "docs/PLUGINS.md", "registry.html"):
        p = ROOT / rel
        if not p.exists():
            continue
        for n in re.findall(r"\b(\d+) (?:memory |MCP )?tools\b",
                            p.read_text(encoding="utf-8")):
            assert int(n) == count, (rel, n, count)


# ------------------------------------------------------------- Oh My Pi -----
def test_the_omp_extension_declares_what_each_tool_does():
    """omp's ToolDefinition treats a missing `approval` as "exec" and a
    missing `loadMode` on an extension tool as "discoverable" (removed from
    the top-level tool list, found by search). Recall must not be gated
    like running code, and a tool the model is told to call before
    answering must be in front of it."""
    src = (ROOT / "integrations/omp/extension.ts").read_text(encoding="utf-8")
    tools = {}
    for block in src.split("pi.registerTool({")[1:]:
        name = re.search(r'name:\s*"([^"]+)"', block).group(1)
        head = block.split("async execute")[0]
        tools[name] = dict(re.findall(r'\b(approval|loadMode):\s*"([^"]+)"', head))
    assert tools == {
        "memory_search": {"approval": "read", "loadMode": "essential"},
        "memory_store": {"approval": "write", "loadMode": "essential"},
    }


# ---------------------------------------------------- Cline (llms-install) --
def test_llms_install_gets_an_agent_past_the_common_install_failures():
    """Cline's marketplace installs by handing this file to the agent, which
    then meets what users meet: pip refusing on an OS-managed Python
    (PEP 668), a Windows shell, a GUI client that cannot see the PATH."""
    from compartment import clients
    text = (ROOT / "llms-install.md").read_text(encoding="utf-8")
    assert "externally-managed-environment" in text
    assert "pipx install compartment" in text and "uv tool install compartment" in text
    # --break-system-packages appears only as something not to do
    for line in text.splitlines():
        if "--break-system-packages" in line:
            assert "do not" in line.lower(), line
    assert "py -m pip install compartment" in text
    assert "where compartment" in text and "which compartment" in text
    for client in re.findall(r"compartment integrate ([a-z0-9][a-z0-9-]*)", text):
        assert client in clients.CLIENTS, client


# ---------------------------------- the repository root is a plugin root ----
#: What each host that installs the repository root loads from it without
#: being told to. skills/ and mcp.json are shared on purpose; everything
#: else here would be read by several hosts in several incompatible ways
#: (a Claude hooks/hooks.json is not a Gemini one; Gemini's commands/ are
#: TOML, Claude's Markdown; OpenClaw imports a root settings.json into its
#: own settings). Adding one is a decision for every host at once, which is
#: why this test names them.
HOST_AUTOLOADED = ("commands", "agents", "hooks", "policies", "output-styles",
                   "monitors", "themes", ".mcp.json", ".lsp.json",
                   "settings.json", ".codex-plugin", ".cursor-plugin",
                   "openclaw.plugin.json")


def test_the_root_holds_nothing_a_host_would_load_by_surprise():
    present = [n for n in HOST_AUTOLOADED if (ROOT / n).exists()]
    assert not present, (
        f"{present} at the repository root would be auto-loaded by Claude "
        "Code, Gemini CLI, Codex, OpenClaw or Hermes, each in its own format; "
        "see HOST_AUTOLOADED and docs/PLUGINS.md before adding it")


def test_every_shared_skill_meets_the_agent_skills_specification():
    """All five hosts load skills/*/SKILL.md, and Hermes and Codex skip a
    skill that fails the specification instead of loading it loosely."""
    allowed = {"name", "description", "license", "compatibility", "metadata",
               "allowed-tools"}
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert skills
    for path in skills:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front = text.split("---\n", 2)[1]
        keys = set(re.findall(r"^([A-Za-z][\w-]*):", front, re.M))
        assert keys <= allowed, (path, keys - allowed)
        name = re.search(r"^name:\s*(\S+)", front, re.M).group(1)
        assert name == path.parent.name
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
        desc = re.search(r"^description:\s*(.+)$", front, re.M).group(1)
        assert 1 <= len(desc) <= 1024
        # metadata is a map of strings: quoted, never a YAML list or number
        for value in re.findall(r"^  [\w-]+:\s*(.*)$", front, re.M):
            assert value.startswith('"') and value.endswith('"'), (path, value)


# --------------------------------------------------- Claude Code, Gemini ----
def test_the_claude_code_marketplace_serves_the_plugin_at_the_root():
    market = _json(".claude-plugin/marketplace.json")
    plugin = _json(".claude-plugin/plugin.json")
    assert market["owner"]["name"]
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] == "compartment"
    assert entry["source"] == "./"
    assert re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", plugin["name"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])
    server = plugin["mcpServers"]["compartment"]
    assert set(server) <= {"type", "command", "args", "env"}


def test_the_gemini_extension_is_what_the_gallery_indexes():
    """The gallery crawler wants gemini-extension.json at the repository
    root with a name matching the install directory, a version and a
    description; extensions may not set `trust` on a server."""
    d = _json("gemini-extension.json")
    assert d["name"] == "compartment"
    assert re.fullmatch(r"\d+\.\d+\.\d+", d["version"]) and d["description"]
    assert (ROOT / d["contextFileName"]).is_file()
    for server in d["mcpServers"].values():
        assert "trust" not in server


def test_gemini_md_is_the_handshake_instructions_verbatim():
    """Gemini CLI loads GEMINI.md into every session as the extension's
    context, on top of the instructions the server sends in its handshake;
    two versions of the rules would tell the model two things."""
    from compartment import server
    flat = lambda s: re.sub(r"\s+", " ", s).strip()        # noqa: E731
    body = (ROOT / "GEMINI.md").read_text(encoding="utf-8").split("\n", 1)[1]
    assert flat(body) == flat(server.COMPARTMENT_INSTRUCTIONS)


def test_the_website_does_not_publish_the_plugin_files_as_pages():
    """GitHub Pages builds the repository root with Jekyll, which renders
    any file with YAML front matter (every SKILL.md) as a page and copies
    the rest; the manifests are for hosts, not for the site."""
    import pytest
    yaml = pytest.importorskip("yaml")
    excluded = set(yaml.safe_load((ROOT / "_config.yml").read_text(
        encoding="utf-8"))["exclude"])
    for name in ("skills", "plugin.json", "mcp.json", "gemini-extension.json",
                 "GEMINI.md", "server.json", "glama.json", "packaging", "tools"):
        assert name in excluded, name
