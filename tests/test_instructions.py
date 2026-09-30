"""Compartment must tell the host WHEN to recall and WHEN to store - not just what
the tools do. Three layers: the MCP `instructions=` handshake string, the
Hermes provider's system-prompt block, and the managed CLAUDE.md block.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_mcp_server_advertises_instructions():
    from compartment.server import mcp, COMPARTMENT_INSTRUCTIONS
    # the string rides the MCP initialize handshake
    assert mcp.instructions == COMPARTMENT_INSTRUCTIONS
    i = COMPARTMENT_INSTRUCTIONS.lower()
    assert "memory_search" in i and "memory_store" in i
    # tells the model WHEN, and which categories to capture
    for kw in ("recall", "store", "credential", "api key", "password",
               "address", "preferences", "decision"):
        assert kw in i, f"instructions missing: {kw}"
    # the data-not-instructions boundary is preserved and emphatic
    assert "data" in i and "never act on it" in i
    # the vault passphrase must never transit tool args
    assert "passphrase into a tool call" in i


def test_store_tool_docstring_says_when():
    from compartment.server import memory_store, memory_search
    ds = (memory_store.__doc__ or "").lower()
    assert "credential" in ds and ("api key" in ds or "api keys" in ds)
    # and what NOT to store, not only what to
    assert "not transient chatter" in " ".join(ds.split())
    sr = (memory_search.__doc__ or "").lower()
    assert "before answering" in sr and "data, not" in sr


def _flat(text: str) -> str:
    return " ".join(text.split())


def _string_constants(path: pathlib.Path) -> list[str]:
    """Every string literal in a Python file, adjacent literals already joined
    - the parser folds "a" "b" into one constant - so a sentence wrapped over
    several source lines is still found whole."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_every_surface_ships_the_same_store_rule():
    """One sentence decides what agents store, and it is shipped in many
    places: the handshake, the tool description, the CLAUDE.md block, the
    Hermes provider's prompt and tool, the skill, GEMINI.md and the Oh My Pi
    tool. Copies drift. Every one of them must carry the rule word for word -
    a host told something different from the others stores differently."""
    from compartment import cli, gate, server
    # The owner's sentence, word for word. Everything below compares against
    # the constant, so this is what stops the constant itself from drifting.
    assert _flat(gate.STORE_RULE) == (
        "STORE anything worth referencing again in future work: names, "
        "addresses, contacts, account IDs, passwords, API keys and other "
        "credentials, lasting file locations and configuration, preferences, "
        "and every durable fact or decision reached. Not transient chatter, "
        "one-off trivia, things freely available on the internet, or the "
        "working details of a task in progress, such as run results, errors, "
        "log contents and temporary paths or settings.")
    rule = _flat(gate.STORE_RULE)

    assert rule in _flat(server.COMPARTMENT_INSTRUCTIONS)
    assert rule in _flat(server.memory_store.__doc__ or "")
    assert rule in _flat(cli._CLAUDE_MD_BODY)

    for copy in (ROOT / "src" / "compartment" / "data" / "hermes-plugin"
                 / "__init__.py",
                 ROOT / "integrations" / "hermes" / "compartment"
                 / "__init__.py"):
        found = [s for s in _string_constants(copy) if rule in _flat(s)]
        # the system-prompt block AND the compartment_store tool description
        assert len(found) == 2, (copy, len(found))

    for doc in (ROOT / "GEMINI.md",
                ROOT / "skills" / "compartmentalize" / "SKILL.md",
                ROOT / "src" / "compartment" / "data" / "agent-skill"
                / "SKILL.md",
                ROOT / "integrations" / "omp" / "extension.ts"):
        assert rule in _flat(doc.read_text(encoding="utf-8")), doc

    # and the old wording is gone everywhere, so no host is told both
    for doc in (ROOT / "GEMINI.md",
                ROOT / "src" / "compartment" / "data" / "agent-skill"
                / "SKILL.md",
                ROOT / "src" / "compartment" / "data" / "hermes-plugin"
                / "__init__.py"):
        flat = _flat(doc.read_text(encoding="utf-8"))
        assert "STORE the moment something worth referencing" not in flat
        assert "when in doubt, store it" not in flat
        assert "the moment information worth" not in flat


def test_hermes_plugin_prompts_when_to_store():
    txt = (ROOT / "src" / "compartment" / "data" / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
    # system-prompt block names the categories and the recall-first habit
    assert "API keys" in txt and "credentials" in txt
    assert "Store with compartment_store" in txt
    assert "recall explicitly" in txt or "compartment_search to recall" in txt
    assert "not\n" in txt or "not instructions" in txt.replace("\n", " ")
    # both bundled copies stay byte-identical (guarded elsewhere too)
    a = (ROOT / "integrations" / "hermes" / "compartment" / "__init__.py").read_bytes()
    b = (ROOT / "src" / "compartment" / "data" / "hermes-plugin" / "__init__.py").read_bytes()
    assert a == b


def test_managed_claude_md_is_idempotent_and_preserves_user_text(tmp_path, monkeypatch):
    from compartment import cli
    md = tmp_path / "CLAUDE.md"
    md.write_text("# My own notes\nkeep this line\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_MD", str(md))

    cli._write_managed_claude_md()
    t1 = md.read_text(encoding="utf-8")
    assert "keep this line" in t1                 # user text untouched
    assert cli._CLAUDE_MD_BEGIN in t1 and cli._CLAUDE_MD_END in t1
    assert "memory_store" in t1 and "memory_search" in t1 and "API keys" in t1
    # the tool line after the rule names the tool, not the excluded things
    assert "Store with `memory_store`" in t1 and "Save it with" not in t1

    # second run updates in place, never duplicates
    cli._write_managed_claude_md()
    t2 = md.read_text(encoding="utf-8")
    assert t2.count(cli._CLAUDE_MD_BEGIN) == 1
    assert t2.count(cli._CLAUDE_MD_END) == 1
    assert "keep this line" in t2


def test_managed_block_created_when_no_file(tmp_path, monkeypatch):
    from compartment import cli
    md = tmp_path / "sub" / "CLAUDE.md"      # parent doesn't exist yet
    monkeypatch.setenv("CLAUDE_MD", str(md))
    cli._write_managed_claude_md()
    assert md.exists() and cli._CLAUDE_MD_BEGIN in md.read_text(encoding="utf-8")
