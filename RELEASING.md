# Releasing Compartment

## Every release ships every download

A release offers **all** of the ways to get Compartment, never just the newest or
most convenient one. Someone arriving at the release page should not have to
know which artifact they need, go hunting through PyPI, or find that the only
link is for a platform they are not on.

Attach all four, and list them together in the notes:

| Artifact | For |
|---|---|
| `Compartment-<version>.pkg` | macOS, one click, with the optional menu bar utility |
| `Compartment-<version>.dmg` | macOS, drag to Applications |
| `compartment-<version>-py3-none-any.whl` | pip / offline installs, any OS |
| `compartment-<version>.tar.gz` | source distribution, packagers, air-gapped builds |

GitHub adds "Source code (zip/tar.gz)" on its own. The notes should also carry
the `pip install compartment` line for people who want the CLI and MCP
server without the app.

## Steps

```bash
# 1. version in lockstep, or the PyPI publish silently no-ops
#    (publish.yml uses skip-existing, so a release that reuses a version
#    uploads nothing and the README on PyPI never updates)
#
#      pyproject.toml                  version    = "2.1"
#      src/compartment/__init__.py     __version__ = "2.1"
#      server.json   packages[0].version = "2.1"    the string PyPI serves
#      server.json   version             = "2.1.0"  the SEMVER form
#      plugin.json   version             = "2.1.0"  Agent Plugins manifest
#      gemini-extension.json version       = "2.1.0"  Gemini CLI extension manifest
#
#    The two server.json fields are deliberately NOT the same value. Step 5
#    explains why; setting them both to "2.1" is what published a registry
#    entry that never became latest.
#
#      integrations/hermes/compartment/plugin.yaml   version: "2.1"
#      src/compartment/data/hermes-plugin/plugin.yaml  version: "2.1"
#
#    plugin.json and the two plugin.yaml copies are the easiest to forget,
#    because nothing downstream fails when they drift. They are what a
#    portable host prints in its own plugin list - `hermes plugins list`
#    shows it in the Version column - so a stale number there tells the user
#    they are running something they are not. That is not hypothetical: the
#    plugin.yaml pair sat at 1.7.0 while the package shipped 4.9.2.
#
#    tests/test_version_lockstep.py now fails when any of them disagree, so
#    this list is a gate rather than a thing to remember.

python -m pytest -q                          # must be green

# 2. python artifacts
rm -rf dist && python -m build

# 3. macOS app + installers (needs a NON-framework Python; see the script)
python tools/build_macos_app.py --pkg --dmg

# 4. tag, publish, attach EVERYTHING
gh release create vX.Y.Z --title "…" --notes-file NOTES.md \
    build/Compartment-X.Y.Z.pkg \
    build/Compartment-X.Y.Z.dmg \
    dist/compartment-X.Y.Z-py3-none-any.whl \
    dist/compartment-X.Y.Z.tar.gz

# 5. the MCP registry entry tracks the version too. Three things bite here.
#
#    a) The registry picks "latest" by SEMVER ordering, at publish time. Our
#       version numbers are plain (2, 2.1) and plain numbers are not valid
#       semver, so a bare "2.1" does NOT outrank an older "1.15.0" and the
#       old entry keeps isLatest. Hence the split in step 1: `version` gets
#       the semver form, `packages[0].version` gets the string PyPI serves.
#       The two fields are independent and the registry treats them so.
#    b) Marking a version deprecated does NOT recompute isLatest. A
#       deprecated version can still be flagged latest. Deprecation alone
#       never repairs a bad pin; only publishing a higher semver does.
#    c) Published versions are immutable. A wrong pin cannot be edited, only
#       superseded, so it is worth getting right the first time.
#
#    The registry token expires within minutes, so log in immediately before
#    every publish or status call, even if you logged in one command ago.
mcp-publisher login github --token "$(gh auth token)" \
    && mcp-publisher validate && mcp-publisher publish
```

## Verify, do not assume

- **The release is not a draft.** A large asset upload that times out leaves
  the release unpublished and untagged - the page reads `untagged-…` and every
  download link 404s. Check: `gh release view vX.Y.Z --json isDraft,tagName`.
- **The download actually serves.** `curl -sIL <asset-url> | head -1` should be
  `HTTP/2 200`, with a `content-length` matching the artifact.
- **PyPI picked it up.** The release triggers `publish.yml`; confirm the new
  version appears at pypi.org/project/compartment.
- **The registry hands out a pin that installs.** Whichever entry carries
  `isLatest: true` must name a PyPI version that exists, or every
  `uvx compartment@<version> serve` fails outright:

  ```bash
  curl -s https://registry.modelcontextprotocol.io/v0.1/servers/\
  io.github.MaxFreedomPollard%2Fcompartment/versions
  # find isLatest true, then check its packages[0].version:
  curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/compartment/<that>/json
  ```

Uploading ~250 MB of installers takes minutes; run it in the background rather
than letting a foreground timeout kill it half way.
