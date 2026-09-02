## What this changes

<!-- One paragraph. What changed, and the situation that made it necessary. -->

## How it was verified

- [ ] `pytest -q` passes locally
- [ ] Ran the affected path against a scratch vault (`--vault /tmp/x.vault`, scratch `HOME`)
- [ ] Tested on: <!-- macOS / Linux / Windows, Python version -->

## Guarantees kept

- [ ] No network call at runtime, no open port
- [ ] No plaintext written to disk
- [ ] No new dependency that is not a pure wheel on macOS, Linux and Windows
- [ ] README / FORMAT.md / SECURITY.md still tell the truth
