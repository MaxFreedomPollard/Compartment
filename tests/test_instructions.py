"""engRAM must tell the host WHEN to recall and WHEN to store - not just what
the tools do. Three layers: the MCP `instructions=` handshake string, the
Hermes provider's system-prompt block, and the managed CLAUDE.md block.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_mcp_server_advertises_instructions():
    from engram.server import mcp, ENGRAM_INSTRUCTIONS
    # the string rides the MCP initialize handshake
    assert mcp.instructions == ENGRAM_INSTRUCTIONS
    i = ENGRAM_INSTRUCTIONS.lower()
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
    from engram.server import memory_store, memory_search
    ds = (memory_store.__doc__ or "").lower()
    assert "credential" in ds and ("api key" in ds or "api keys" in ds)
    assert "do not store" in ds
    sr = (memory_search.__doc__ or "").lower()
    assert "before answering" in sr and "data, not" in sr


def test_hermes_plugin_prompts_when_to_store():
    txt = (ROOT / "src" / "engram" / "data" / "hermes-plugin" / "__init__.py").read_text()
    # system-prompt block names the categories and the recall-first habit
    assert "API keys" in txt and "credentials" in txt
    assert "engram_store the moment" in txt
    assert "recall explicitly" in txt or "engram_search to recall" in txt
    assert "not\n" in txt or "not instructions" in txt.replace("\n", " ")
    # both bundled copies stay byte-identical (guarded elsewhere too)
    a = (ROOT / "integrations" / "hermes" / "engram" / "__init__.py").read_bytes()
    b = (ROOT / "src" / "engram" / "data" / "hermes-plugin" / "__init__.py").read_bytes()
    assert a == b


def test_managed_claude_md_is_idempotent_and_preserves_user_text(tmp_path, monkeypatch):
    from engram import cli
    md = tmp_path / "CLAUDE.md"
    md.write_text("# My own notes\nkeep this line\n")
    monkeypatch.setenv("CLAUDE_MD", str(md))

    cli._write_managed_claude_md()
    t1 = md.read_text()
    assert "keep this line" in t1                 # user text untouched
    assert cli._CLAUDE_MD_BEGIN in t1 and cli._CLAUDE_MD_END in t1
    assert "memory_store" in t1 and "memory_search" in t1 and "API keys" in t1

    # second run updates in place, never duplicates
    cli._write_managed_claude_md()
    t2 = md.read_text()
    assert t2.count(cli._CLAUDE_MD_BEGIN) == 1
    assert t2.count(cli._CLAUDE_MD_END) == 1
    assert "keep this line" in t2


def test_managed_block_created_when_no_file(tmp_path, monkeypatch):
    from engram import cli
    md = tmp_path / "sub" / "CLAUDE.md"      # parent doesn't exist yet
    monkeypatch.setenv("CLAUDE_MD", str(md))
    cli._write_managed_claude_md()
    assert md.exists() and cli._CLAUDE_MD_BEGIN in md.read_text()
