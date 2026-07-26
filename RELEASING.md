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
| `compartment_memory_vault-<version>-py3-none-any.whl` | pip / offline installs, any OS |
| `compartment_memory_vault-<version>.tar.gz` | source distribution, packagers, air-gapped builds |

GitHub adds "Source code (zip/tar.gz)" on its own. The notes should also carry
the `pip install compartment` line for people who want the CLI and MCP
server without the app.

## Steps

```bash
# 1. version in lockstep - all three, or the PyPI publish silently no-ops
#    (publish.yml uses skip-existing, so a release that reuses a version
#    uploads nothing and the README on PyPI never updates)
#      pyproject.toml    version = "X.Y.Z"
#      src/compartment/__init__.py    __version__ = "X.Y.Z"
#      server.json       both "version" fields

python -m pytest -q                          # must be green

# 2. python artifacts
rm -rf dist && python -m build

# 3. macOS app + installers (needs a NON-framework Python; see the script)
python tools/build_macos_app.py --pkg --dmg

# 4. tag, publish, attach EVERYTHING
gh release create vX.Y.Z --title "…" --notes-file NOTES.md \
    build/Compartment-X.Y.Z.pkg \
    build/Compartment-X.Y.Z.dmg \
    dist/compartment_memory_vault-X.Y.Z-py3-none-any.whl \
    dist/compartment_memory_vault-X.Y.Z.tar.gz

# 5. the MCP registry entry tracks the version too. The registry token expires
#    quickly, so log in immediately before publishing.
mcp-publisher login github --token "$(gh auth token)" && mcp-publisher publish
```

## Verify, do not assume

- **The release is not a draft.** A large asset upload that times out leaves
  the release unpublished and untagged - the page reads `untagged-…` and every
  download link 404s. Check: `gh release view vX.Y.Z --json isDraft,tagName`.
- **The download actually serves.** `curl -sIL <asset-url> | head -1` should be
  `HTTP/2 200`, with a `content-length` matching the artifact.
- **PyPI picked it up.** The release triggers `publish.yml`; confirm the new
  version appears at pypi.org/project/compartment.

Uploading ~250 MB of installers takes minutes; run it in the background rather
than letting a foreground timeout kill it half way.
