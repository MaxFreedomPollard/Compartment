"""What `tools/list` tells a client about each memory tool: the hints it
uses to decide how to present and gate a tool, and input schemas that the
strictest clients' validators take.

Listed through the SDK the server runs on (mcp 1.x or 2.x), without an
event loop, as test_server.py calls tools: listing awaits nothing."""
import pytest

from compartment import server


def _listed():
    call = server.mcp.list_tools()
    try:
        call.send(None)
    except StopIteration as done:
        return done.value
    finally:
        call.close()
    raise AssertionError("list_tools waited for an event loop")


def _dump(tool) -> dict:
    return tool.model_dump(by_alias=True, exclude_none=True)


def test_every_tool_says_it_stays_on_this_machine():
    """openWorldHint absent means true in the spec: "may interact with an
    open world of external entities". Every tool left it out, so a client
    that weighs the hint treated a fully offline vault as an internet
    tool."""
    tools = [_dump(t) for t in _listed()]
    assert len(tools) == 14
    for t in tools:
        hints = t["annotations"]
        assert hints["openWorldHint"] is False, t["name"]
        assert hints["title"] == t["title"], t["name"]
        assert isinstance(hints["readOnlyHint"], bool), t["name"]


def test_the_hints_match_what_each_tool_does():
    hints = {t["name"]: t["annotations"] for t in map(_dump, _listed())}
    for name in ("memory_search", "memory_get", "memory_recent",
                 "memory_relations", "memory_list_namespaces",
                 "memory_status", "memory_selftest"):
        assert hints[name]["readOnlyHint"] is True, name
    for name in ("memory_forget", "memory_unlink"):
        assert hints[name]["destructiveHint"] is True, name
    for name in ("memory_link", "memory_unlink", "memory_forget",
                 "memory_lock", "memory_unlock"):
        assert hints[name].get("idempotentHint") is True, name
    for name in ("memory_store", "memory_store_many"):
        assert not hints[name].get("idempotentHint"), name


def _typed(schema: dict, where: str) -> None:
    """Every schema node carries a type, directly or in each branch of an
    anyOf: Gemini's and older Codex's converters dropped or defaulted an
    untyped node, and none of them resolve $ref."""
    assert "$ref" not in schema and "$defs" not in schema, where
    if "anyOf" in schema:
        assert schema["anyOf"], where
        for i, branch in enumerate(schema["anyOf"]):
            _typed(branch, f"{where}.anyOf[{i}]")
        return
    assert "type" in schema, where
    if schema["type"] == "array":
        _typed(schema["items"], f"{where}.items")
    if schema["type"] == "object":
        for key, sub in schema.get("properties", {}).items():
            _typed(sub, f"{where}.{key}")


def test_every_input_schema_is_one_strict_clients_accept():
    """An object at the top, with no union there (OpenAI and Gemini refuse
    one), every property typed, and every required name defined."""
    for t in map(_dump, _listed()):
        schema = t["inputSchema"]
        assert schema["type"] == "object", t["name"]
        for key in ("anyOf", "oneOf", "allOf", "not", "$ref"):
            assert key not in schema, (t["name"], key)
        _typed(schema, t["name"])
        assert set(schema.get("required", [])) <= set(schema["properties"])


def test_mcp_1x_does_not_log_every_request(capsys):
    """mcp 1.x's FastMCP logs at INFO by default, "Processing request of type
    CallToolRequest" on stderr for every request, which Cursor shows as an
    [error] line each time. mcp 2.x logs nothing per request."""
    import logging
    if not server.MCPServer.__module__.startswith("mcp.server.fastmcp"):
        pytest.skip("mcp 2.x logs nothing per request")
    assert server.mcp.settings.log_level == "WARNING"
    logging.getLogger("mcp.server.lowlevel.server").info(
        "Processing request of type ListToolsRequest")
    assert "Processing request" not in capsys.readouterr().err
