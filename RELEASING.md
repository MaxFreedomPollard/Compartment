# Releasing Compartment

## Every release ships every download

A release offers **all** of the ways to get Compartment, never just the newest or
most convenient one. Someone arriving at the release page should not have to
know which artifact they need, go hunting through PyPI, or find that the only
link is for a platform they are not on.

Attach all of them, and list them together in the notes. The Release
workflow (`.github/workflows/release.yml`) builds every one from the tag on
GitHub's machines and attaches them to a draft, so nobody has to remember:

| Artifact | For |
|---|---|
| `Compartment-<version>.pkg` | macOS on Apple silicon, one click, with the optional menu bar utility |
| `Compartment-<version>.dmg` | macOS on Apple silicon, drag to Applications |
| `Compartment-<version>-intel.pkg` | macOS on Intel, one click |
| `Compartment-<version>-intel.dmg` | macOS on Intel, drag to Applications |
| `compartment-<version>-py3-none-any.whl` | pip / offline installs, any OS |
| `compartment-<version>.tar.gz` | source distribution, packagers, air-gapped builds |
| `SHA256SUMS.txt` | the checksums of everything above |

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

# 2. merge the release PR, then tag the merge commit and push the tag.
#    That is the whole build: release.yml checks the tag against every
#    version field, builds the wheel and sdist, builds the macOS app, .pkg
#    and .dmg on an Apple silicon runner AND an Intel one, and attaches all
#    of them plus SHA256SUMS.txt to a DRAFT release. Release notes come from
#    .github/release-notes/vX.Y.Z.md when that file exists.
git tag vX.Y.Z && git push origin vX.Y.Z

# 3. review the draft on the Releases page, then press Publish. Publishing
#    triggers publish.yml, which uploads the release's own wheel and sdist to
#    PyPI (the same bytes as the downloads), and publish-registry.yml.
#    A rebuild of an existing tag: Actions -> Release -> Run workflow -> tag.
#
#    Building by hand still works when it has to, from a Mac:
#      rm -rf dist && python -m build
#      python tools/build_macos_app.py --pkg --dmg   # NON-framework Python; see the script
#      gh release create vX.Y.Z --draft --notes-file NOTES.md build/*.pkg build/*.dmg dist/*

# 4. nothing to do by hand for PyPI or the registry; see "Verify" below.

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
#    publish-registry.yml does this on Publish, authenticated by OIDC. By
#    hand only when that workflow failed: the registry token expires within
#    minutes, so log in immediately before every publish or status call,
#    even if you logged in one command ago.
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
