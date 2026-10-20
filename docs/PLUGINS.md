# Plugins, extensions and store listings

Compartment is one Python package on PyPI and one MCP server
(`compartment serve`, stdio, 14 memory tools). Everything below is a way for a
particular host or catalogue to find that server and start it. This page says,
store by store, what the listing is, how a user installs from it, and what the
maintainer has to do to publish or update it. The formats were checked against
each host's own repository in October 2026; the source is named for each.

`tests/test_plugin_manifests.py` checks every manifest here against the parts
of its host's format that matter, and against each other (same command, same
subcommand, one caller per host, no wrong tool count). `tests/test_version_lockstep.py`
fails the build when any manifest carrying a version disagrees with the package,
or when a file anywhere in the tree carries a version it does not check.

## At a glance

| Store / host | Listing in this repo | User installs with | Maintainer, each release | Maintainer, once |
|---|---|---|---|---|
| PyPI | `pyproject.toml` | `pip install compartment` | automatic (`publish.yml`) | - |
| Official MCP Registry | `server.json` | their client's registry browser, `uvx compartment serve` | automatic (`publish-registry.yml`) | - |
| Claude Code plugins | `.claude-plugin/` | `/plugin marketplace add MaxFreedomPollard/Compartment` | version is gated; nothing else | submit to Anthropic's plugin directory |
| Claude Desktop extension | `packaging/mcpb/` | the `.mcpb` from the release | automatic (`publish.yml` builds and attaches it once PyPI has the version) | submit to Anthropic's extension directory |
| Codex plugins | root `plugin.json` + `.claude-plugin/marketplace.json` | `codex plugin marketplace add MaxFreedomPollard/Compartment` | nothing | - |
| Gemini CLI extensions | `gemini-extension.json`, `GEMINI.md` | `gemini extensions install https://github.com/MaxFreedomPollard/Compartment` | nothing (crawled daily) | - (the `gemini-cli-extension` topic is set) |
| Hermes Agent | `integrations/hermes/`, root `plugin.json` + `mcp.json` | `compartment integrate hermes` or `hermes plugins install MaxFreedomPollard/Compartment` | nothing | upstream PR (see below) |
| OpenClaw | `.claude-plugin/` (as a bundle), `skills/` | `compartment integrate openclaw` | nothing | publish the skill on ClawHub |
| Smithery | the `.mcpb` | Smithery's install command | `smithery mcp publish` | `smithery auth login` |
| Glama | `glama.json`, `Dockerfile` | Glama's page | nothing | - |
| Docker MCP Catalog | `packaging/docker-mcp/server.yaml` | Docker Desktop, MCP Toolkit | nothing (Docker bumps the pin) | PR to docker/mcp-registry |
| Cline marketplace | `llms-install.md` | Cline's MCP marketplace | nothing | issue at cline/mcp-marketplace |
| Continue hub | `packaging/continue/compartment.yaml` | hub.continue.dev, or copy the file | republish the block | publish the block |
| goose | `add/goose.html` | the Install button | nothing | PR to goose's extension directory |
| LM Studio | `add/lmstudio.html` | the Add button | nothing | - |
| Cursor | deeplink in the README | the Add to Cursor button | nothing | - |
| VS Code, Copilot | deeplinks in the README, `server.json` | the Install buttons, VS Code's MCP gallery | nothing | - |
| Oh My Pi | `integrations/omp/extension.ts` | copy the file; `compartment integrate omp` | nothing | - |

## The command a host runs, and PATH

Most listings start the server as the bare command `compartment`, because their
formats allow nothing else portable: Agent Plugins (`mcp.json`, read by Codex,
Hermes and OpenClaw) requires a single bare executable name or a path inside
the plugin, and a Claude Code, Gemini, Continue or LM Studio entry has no way
to discover where pip put the script. A bare name works when the host inherits
the shell's PATH, which terminal programs (Claude Code, Codex, Gemini CLI,
Oh My Pi) do. GUI apps started from the Dock, the Start menu or a desktop
launcher often do not, and then the server fails with `command not found` or
`spawn compartment ENOENT`.

Where a format allows something safer, the listing uses it:

- The Claude Desktop bundle runs the server through `uv`, which Claude Desktop
  supplies, in an environment it builds from the bundle; no PATH lookup of
  `compartment` at all.
- goose runs `uvx compartment` (goose only accepts its own launchers from a
  link, and ships uvx).
- The Docker catalogue entry runs the image's own entrypoint.
- `compartment integrate <client>` writes the absolute path of the executable
  into every config it edits, which is the reliable route for GUI clients.

For a hand-written config in a GUI client, use the absolute path:
`which compartment` (macOS, Linux) or `where compartment` (Windows).

## The caller

Every listing passes `--caller <host>`, which names the client in the audit
log and is the key per-caller ACL entries are written against. Where
`compartment integrate` wires the same host, the listing uses the same name
(`claude-code`, `claude-desktop`, `gemini`, `lmstudio`, `goose`, `continue`),
so one app never shows up as two clients. The Agent Plugins package uses
`agent-plugin`, because Codex, Hermes and OpenClaw all read it; the Docker
image uses `docker`. A test enforces all of this.

## Using two routes at once

A host wired two ways starts two servers, and the model sees every tool twice:

- `compartment init` connects Claude Code when it is installed (the same as
  `compartment integrate claude`). Installing the Claude Code plugin as well
  adds a second server. Claude Code deduplicates a plugin server only when its
  command is identical to a configured one, and integrate writes an absolute
  path, so it does not here. Use one: integrate is the fuller route (capture
  hook, CLAUDE.md block, Claude Desktop).
- The same holds for the Claude Desktop extension next to
  `compartment integrate claude`.
- Gemini CLI is the exception: a server in `settings.json` with the same name
  as an extension's replaces it, so `integrate gemini` and the extension do not
  double up.

## Store by store

### Official MCP Registry

- **Listing:** `server.json`, schema `2025-12-11` (the newest released schema
  in modelcontextprotocol/registry), name `io.github.MaxFreedomPollard/compartment`,
  one PyPI package run as `uvx compartment serve`, plus `title`, `websiteUrl`
  and `icons`. Validates against the schema with `mcp-publisher validate`.
- **Users:** clients with a registry browser install it from there. VS Code's
  MCP gallery and GitHub's MCP registry draw on this registry.
- **Each release:** automatic. `publish-registry.yml` publishes on every GitHub
  release after the version reaches PyPI. The two version fields are
  deliberately different (RELEASING.md step 5 explains why).
- The registry proves PyPI ownership by finding `mcp-name: <name>` in the
  README PyPI serves; a test keeps the two in sync.
- Possible later: an `mcpb` package entry pointing at the release's `.mcpb`.
  It needs the bundle's SHA-256 in `server.json` (`fileSha256`), so the release
  workflow would have to write it in before publishing.

### Claude Code plugin marketplace

- **Listing:** `.claude-plugin/marketplace.json` (marketplace `maxfreedompollard`,
  one plugin with `source: "./"`) and `.claude-plugin/plugin.json` (name,
  display name, version, the MCP server). Because the plugin root is the
  repository root, Claude Code also loads `skills/compartmentalize`, which
  appears as `/compartment:compartmentalize`. Format: anthropics/claude-code
  and anthropics/claude-plugins-official, schemas on SchemaStore.
- **Users:** after `pip install compartment && compartment init`:
  `/plugin marketplace add MaxFreedomPollard/Compartment`, then
  `/plugin install compartment@maxfreedompollard`. Updates arrive with
  `/plugin marketplace update maxfreedompollard`. See "Using two routes at
  once" first.
- **Each release:** the `version` in `.claude-plugin/plugin.json` moves with
  the package (gated); Claude Code compares it to decide what is an update.
- **Once:** to appear in `/plugin` > Discover for everyone, submit the plugin
  to Anthropic's directory (anthropics/claude-plugins-official, through the
  submission form its README links, `clau.de/plugin-directory-submission`).
- **Not shipped: the capture hook.** `integrate claude` installs a
  `PostToolUse` hook into `~/.claude/settings.json`, and `compartment init`
  runs that on any machine with Claude Code. A plugin hook would run a second
  copy of `compartment hook capture` on every Write and Edit for nearly every
  plugin user. It cannot live at the root as `hooks/hooks.json` either, because
  Gemini CLI and OpenClaw read that path in their own formats. It can ship once
  the capture command can tell it is the plugin's copy and stand down when
  settings.json already has one (for example a flag that `hook capture`
  accepts and checks with `claude_hooks.is_installed()`); then the hook goes
  inline in `.claude-plugin/plugin.json`.

### Claude Desktop extension (MCP Bundle)

- **Listing:** `packaging/mcpb/` - `manifest.json` (MCPB `manifest_version`
  0.4, server type `uv`), `pyproject.toml` pinning `compartment==<version>`,
  `.python-version`, and `server/main.py`. Format: modelcontextprotocol/mcpb
  (formerly anthropics/dxt). Type `uv` is the only one that can carry
  compiled dependencies (onnxruntime, PyNaCl, argon2-cffi, numpy) to every
  platform: Claude Desktop has uv build the environment from PyPI once, at
  install. The server it starts makes no network connection.
- **Users:** download `compartment-<version>.mcpb` from the release and open
  it, or Claude Desktop > Settings > Extensions > Install Extension. The vault
  still comes from `compartment init` in a terminal (the passphrase has to be
  typed somewhere).
- **Each release:** automatic. Publishing the release runs `publish.yml`,
  whose `claude-desktop-bundle` job waits for the version on PyPI, then
  runs `python tools/build_mcpb.py` (dist/compartment-<version>.mcpb, with
  uv.lock) and attaches it to the release. By hand, the same two commands
  after the version is on PyPI: `pip install uv`, then the build. The
  build refuses a manifest or pin that is not the package version, and is
  reproducible byte for byte. `uv lock` needs the release on PyPI, so this
  runs after `publish.yml`, not before.
- **Once:** Claude Desktop's curated extension directory is Anthropic's;
  submission is through Anthropic, not a GitHub repository.
- The interpreter is pinned (3.12, and `requires-python` below 3.15) because uv
  otherwise picks the newest Python, and onnxruntime has no wheels for 3.15
  yet. Raise both when it does.

### Codex

- **Listing:** Codex reads `.claude-plugin/marketplace.json` (it looks for
  `.agents/plugins/`, then `.claude-plugin/`, then `.cursor-plugin/`). For the
  plugin at the root it loads the Agent Plugins `plugin.json` first, then
  `mcp.json` and `skills/`, and takes its listing (display name, category,
  prompts, logo) from `extensions["com.openai"]` in that manifest. Format:
  openai/codex `codex-rs/core-plugins`.
- **Users:** `codex plugin marketplace add MaxFreedomPollard/Compartment`, then
  `codex plugin add compartment@maxfreedompollard`.
- **Each release:** nothing beyond the gated version.
- OpenAI's curated directory (openai/plugins) has no public submission
  process as of October 2026.

### Gemini CLI

- **Listing:** `gemini-extension.json` (name, version, description,
  `contextFileName: GEMINI.md`, the MCP server). Gemini also loads `skills/`
  from the extension. `GEMINI.md` is the server's handshake instructions,
  verbatim (tested). Format: google-gemini/gemini-cli `docs/extensions/`.
- **Users:** `gemini extensions install https://github.com/MaxFreedomPollard/Compartment`.
  Gemini only starts stdio servers in trusted folders (`gemini trust`).
- **Each release:** nothing; the gallery crawler indexes tagged releases daily.
  Installs from a release use the source archive as long as the release has
  more than one asset. A release with exactly one asset would make Gemini
  download that asset as the extension, so keep the full set attached.
- **Once:** the `gemini-cli-extension` topic on the GitHub repository
  (About > Topics) is the gallery's only requirement beyond the manifest at
  the root; it is set.

### Hermes Agent

- **Listing:** the native memory provider in `integrations/hermes/` (the
  fuller route) and the portable Agent Plugins package at the root
  (`plugin.json` 1.0.0, `mcp.json`, `skills/`). Agent Plugins 1.0.0 is still
  the published release; 1.1.0 is a draft no host accepts.
- **Users:** `compartment integrate hermes`, or
  `hermes plugins install MaxFreedomPollard/Compartment`.
- **Once:** the upstream PR described in `integrations/hermes/upstream/PR.md`.
  Its `plugin.yaml` there is stale (version 1.7.0, `compartment>=2.3`); the
  lockstep test pins it as a known stale copy until it is refreshed.

### OpenClaw

- **Listing:** OpenClaw installs Agent Plugins, Claude, Codex and Cursor
  bundles. This repository is detected as a Claude bundle (a client marker
  wins over the root `plugin.json`): it gets `skills/` and the MCP server from
  `.claude-plugin/plugin.json`. Format: openclaw/openclaw `docs/plugins/bundles.md`.
- **Users:** `compartment integrate openclaw` (writes the server with caller
  `openclaw` and installs the skill), or
  `openclaw plugins install git:github.com/MaxFreedomPollard/Compartment`
  (then the caller is `claude-code`, the bundle's).
- **Once:** publish `skills/compartmentalize` to ClawHub with the `clawhub`
  CLI (needs a ClawHub account). A native memory-slot plugin
  (`openclaw.plugin.json`) needs in-process TypeScript and is not written;
  adding that manifest at the root would make OpenClaw stop treating the
  repository as a bundle.

### Smithery

- **Listing:** Smithery no longer builds local servers from a `smithery.yaml`;
  local servers are published as MCP Bundles. Use the release's `.mcpb`
  (Smithery's limit is 25 MB; ours is about 40 KB).
- **Each release / once:** `smithery auth login`, then
  `smithery mcp publish dist/compartment-<version>.mcpb -n maxfreedompollard/compartment`.

### Glama

- **Listing:** `glama.json` (schema and maintainers); Glama builds the
  `Dockerfile` to inspect the server. Already listed; nothing to do.

### Docker MCP Catalog

- **Listing:** `packaging/docker-mcp/server.yaml`, the entry docker/mcp-registry
  wants at `servers/compartment/server.yaml`. Docker builds and signs
  `mcp/compartment` from the `Dockerfile`, mounts the user's vault folder at
  `/data`, takes the passphrase from Docker Desktop's secret store as
  `COMPARTMENT_PASSPHRASE`, and runs the container with no network.
- **Once:** fork docker/mcp-registry, copy the file to
  `servers/compartment/server.yaml` (the header comment can go), run
  `task validate -- --name compartment` and `task build -- --tools compartment`,
  and open the PR with their template. The server lists its tools without a
  vault, so no `tools.json` is needed; add one only if their build cannot
  start the image. After the merge, Docker's pin-update workflow moves
  `source.commit` with each release.
- **Users:** create the vault on the host first (`compartment init`). On
  Linux the image's user (uid 10001) must be able to read the mounted vault,
  whose files are mode 0600; Docker Desktop on macOS and Windows maps
  ownership for you.

### Cline marketplace

- **Listing:** `llms-install.md`, the instructions Cline's agent follows to
  install (including PEP 668 systems, Windows and PATH problems).
- **Once:** open an issue at github.com/cline/mcp-marketplace with the
  repository URL, `docs/images/logo-400.png` (400x400 PNG) and the reason;
  test that Cline can install from `llms-install.md` first.

### Continue hub

- **Listing:** `packaging/continue/compartment.yaml`, a Continue block
  (`schema: v1`). Users can also copy it into `.continue/mcpServers/` (one
  workspace) or `~/.continue/mcpServers/` (all of them).
- **Each release / once:** publish the block on hub.continue.dev (needs an
  account). Its version is gated with the package.

### goose

- **Listing:** `add/goose.html`, a `goose://extension` link running
  `uvx compartment --caller goose serve`.
- **Once:** goose's extension directory is `documentation/static/servers.json`
  in block/goose. A PR adding:

  ```json
  {
    "id": "compartment",
    "name": "Compartment",
    "description": "Encrypted, fully offline long-term memory for AI agents",
    "command": "uvx compartment --caller goose serve",
    "link": "https://github.com/MaxFreedomPollard/Compartment",
    "installation_notes": "Create the encrypted vault once first: pip install compartment && compartment init",
    "is_builtin": false,
    "endorsed": false,
    "environmentVariables": []
  }
  ```

### LM Studio, Cursor, VS Code, Kiro

- **LM Studio:** `add/lmstudio.html`, an `lmstudio://add_mcp` link with the
  entry base64-encoded (format: lmstudio-ai/docs). No directory.
- **Cursor:** the README's `cursor.com/install-mcp` button, and the existing
  Cursor Directory listing. Cursor's own plugin marketplace
  (`.cursor-plugin/`) is curated by Cursor and submitted through the Cursor
  team; a `.cursor-plugin` manifest at the root would also read the root
  `mcp.json`, which is in Agent Plugins format, and would make OpenClaw treat
  the repository as a Cursor bundle, so none is shipped.
- **VS Code / Copilot:** the README's `vscode.dev/redirect/mcp/install`
  buttons; the gallery side comes from the MCP Registry.
- **Kiro:** the README's `kiro.dev/launch/mcp/add` button.

### Oh My Pi

- **Listing:** `integrations/omp/extension.ts`, an omp extension module
  (`ExtensionAPI`): automatic recall and capture, and `memory_search` /
  `memory_store` tools declared with omp's approval tiers (read, write) and
  loaded as essential. Format: can1357/oh-my-pi `docs/extensions.md`.
- **Users:** copy it to `~/.omp/agent/extensions/compartment.ts`;
  `compartment integrate omp` adds the MCP server as well.

### Directories that scrape

mcp.so, MCP Market, LobeHub, mcpservers.org, PulseMCP, MCP Toplist, the
toolsdk registry and the awesome lists read GitHub, PyPI or the MCP Registry.
Nothing in this repository is specific to them.
