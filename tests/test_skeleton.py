"""Bootstrap smoke tests: settings, exception base, import graph."""

from __future__ import annotations

from facebook_mcp.core.config import Settings, get_settings
from facebook_mcp.core.exceptions import FacebookError
from facebook_mcp.server import mcp


def test_settings_defaults() -> None:
    s = get_settings()
    assert s.transport_mode == "auto"
    assert s.impersonate == "chrome"
    assert isinstance(s, Settings)


def test_exception_hierarchy() -> None:
    from facebook_mcp.core.exceptions import AuthError

    assert issubclass(AuthError, FacebookError)


def test_tool_surface_is_covered_elsewhere() -> None:
    """The MCP surface has a single contract test; see test_tools_contract.py."""
    assert mcp.name == "facebook-mcp"
