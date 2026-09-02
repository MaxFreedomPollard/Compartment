# Contributing to Compartment

Thanks for looking under the hood. Compartment is small enough that one
person can hold the whole thing in their head, and it should stay that way:
prefer a change that removes a moving part over one that adds an option.

## Set up

```bash
git clone https://github.com/MaxFreedomPollard/Compartment.git
cd Compartment
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest -q
```

Python 3.11 or newer. The test suite runs in about two minutes and needs no
network: set `COMPARTMENT_ASSERT_OFFLINE=1` and any test that opens a socket
fails, which is exactly how CI runs it on Linux, macOS and Windows.

The dashboard tests (`tests/test_dash.py`) bind 127.0.0.1 by design, so run
those without the guard.

Work against a scratch vault, never your own:

```bash
compartment --vault /tmp/dev.vault init --passphrase dev --no-app
compartment --vault /tmp/dev.vault --caller dev store --source "from chat" "Hello."
```

`init` also wires whichever agents it finds on the machine (Claude Code, Hermes,
OpenClaw). To keep a scratch vault from touching your real agent configs, give
it a scratch `HOME` as well:
`HOME=/tmp/devhome compartment --vault /tmp/dev.vault init --passphrase dev --no-app`.

## What makes a good issue

- **A bug report** that says what you ran, what you expected, what happened,
  your OS and Python version, and `compartment --version`. Never paste a
  passphrase or a vault file. The `Bug report` template asks for exactly this.
- **A feature request** that describes the situation you were in, not the
  button you want. Half of the requests we can't take are solved by a command
  that already exists.
- **Retrieval complaints are welcome and specific is best.** If a search
  should have returned a memory and did not, include the query, the memory
  text, and the `--json` output. Ranking lives in one file
  (`src/compartment/ranking.py`) and every number in it was chosen against a
  measurement; a counter-example is the most useful thing you can send.

Security problems go to [SECURITY.md](SECURITY.md), not the tracker.

## Pull requests

1. One change per PR, with a test that fails without it.
2. Keep the guarantees. A PR must not open a port, add a network call at
   runtime, write plaintext to disk, or add a dependency that is not a pure
   wheel on all three operating systems. CI enforces the first two; review
   enforces the rest.
3. Commit messages say what changed and why, in a sentence
   (`git log` is the changelog).
4. If you touch a user-facing string, check that the README still tells the
   truth. If you touch storage, [FORMAT.md](FORMAT.md) has to change with it.
5. Version numbers are edited only in a release commit; see
   [RELEASING.md](RELEASING.md). `tests/test_version_lockstep.py` fails if the
   copies drift.

Small, obviously-right PRs get merged fast. Large ones start as an issue so
we can agree on the shape first.

## Adding an MCP client

`compartment integrate --list` names every client it knows. Adding one is a
single entry in `src/compartment/clients.py` (config path per OS, JSON key,
transport shape) plus a case in `tests/test_clients.py` that proves the write
merges rather than replaces, takes a backup first, and refuses a config it
cannot parse. That is the whole contract; keep it.

## Memory packs

Packs are signed, read-only bundles of memories (`compartment pack build`).
The starter pack is built by `tools/build_starter_pack.py` from
`tools/starter/starter_facts.jsonl`. A fact in that file is one claim, one or
two sentences, true on any machine. Corrections to starter facts are
straightforward PRs; new packs are best discussed in an issue first, since
every user carries the default pack in RAM.

## Where to talk

Bugs and features: [GitHub Issues](https://github.com/MaxFreedomPollard/Compartment/issues).
Questions and ideas: [GitHub Discussions](https://github.com/MaxFreedomPollard/Compartment/discussions).
