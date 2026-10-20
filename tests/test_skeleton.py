"""M0 skeleton smoke tests: import graph + tool registration."""

from __future__ import annotations

import asyncio

from facebook_mcp.core.config import Settings, get_settings
from facebook_mcp.core.exceptions import FacebookError
from facebook_mcp.server import mcp

EXPECTED_TOOLS = {
    "get_user_latest_post_time",
    "search_people",
    "search_pages",
    "send_message",
}


def test_settings_defaults() -> None:
    s = get_settings()
    assert s.transport_mode == "auto"
    assert s.impersonate == "chrome"
    assert isinstance(s, Settings)


def test_exception_hierarchy() -> None:
    from facebook_mcp.core.exceptions import AuthError

    assert issubclass(AuthError, FacebookError)


def test_tools_registered() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names >= EXPECTED_TOOLS


def test_all_tools_documented() -> None:
    """Every tool must expose a description and typed input schema."""
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} >= EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.inputSchema.get("type") == "object"
