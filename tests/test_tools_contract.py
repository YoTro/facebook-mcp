"""One contract test for the MCP surface.

Tool registration used to be asserted in four files (skeleton, accounts, search,
message) with different expectations — and the skeleton list was missing
`get_user_recent_posts`, so a missing tool could still pass somewhere. This is the
single place that defines the public surface; the per-feature files test behaviour
instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from facebook_mcp.server import mcp

# name -> (required args, optional args)
EXPECTED: dict[str, tuple[set[str], set[str]]] = {
    "get_user_latest_post_time": ({"username"}, {"account"}),
    "get_user_recent_posts": ({"username"}, {"limit", "account"}),
    "search_people": ({"q"}, {"limit", "account"}),
    "search_pages": ({"q"}, {"limit", "account"}),
    "send_message": ({"recipient", "text"}, {"account"}),
}


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_exactly_the_documented_tools_are_registered(tools: dict[str, Any]) -> None:
    assert set(tools) == set(EXPECTED), "the public tool surface changed"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_tool_contract(name: str, tools: dict[str, Any]) -> None:
    tool = tools[name]
    required, optional = EXPECTED[name]
    schema = tool.inputSchema

    assert tool.description, f"{name} has no description"
    assert schema.get("type") == "object"
    properties = set(schema.get("properties", {}))
    assert required | optional <= properties, f"{name} is missing arguments"
    assert set(schema.get("required", [])) == required, f"{name} required args drifted"

    # `account` selects the credential set and must never become mandatory.
    assert "account" in properties
    assert "account" not in schema.get("required", [])

    assert tool.outputSchema, f"{name} has no output schema"


def test_send_message_advertises_the_protocol_path(tools: dict[str, Any]) -> None:
    """Callers decide whether to retry from the description; it must be current."""
    description = tools["send_message"].description
    assert "WebSocket" in description
    assert "no browser is needed" in description
    assert "unknown" in description

    delivery = tools["send_message"].outputSchema["properties"]["delivery"]["description"]
    assert "message_id" in delivery
    assert "unknown" in delivery
