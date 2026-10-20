"""MCP interface layer (L5): tool registration.

Uses the official ``mcp`` SDK (``mcp.server.fastmcp.FastMCP``). This layer only
depends on ``core`` (models, exceptions, accounts) and ``services``; it never
touches transport / session / routes directly.

Every tool accepts an optional ``account`` argument; it selects which
``~/.facebook-mcp/accounts/<account>/`` credential set is used. Omit it to use
``FB_ACCOUNT`` (default ``"default"``).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import __version__
from .core.logger import get_logger
from .core.models import Post, PostTimeResult, SearchResult, SendResult
from .services.factory import get_services

logger = get_logger(__name__)

_ACCOUNT_DOC = (
    "Optional account name selecting the credential set under "
    "~/.facebook-mcp/accounts/<account>/. Defaults to the configured account."
)

mcp = FastMCP(
    name="facebook-mcp",
    instructions=(
        "Facebook automation tools: look up a user's latest post time, "
        "search for people or pages by keyword, and send Messenger messages. "
        "Use for authorized accounts only. Each tool accepts an optional "
        "'account' argument to choose between configured Facebook accounts."
    ),
)

# `mcp<2`'s FastMCP does not expose a version argument, so report our own
# package version instead of the SDK's in `initialize` responses.
try:  # pragma: no cover - SDK internals, guarded on purpose
    mcp._mcp_server.version = __version__
except AttributeError:  # pragma: no cover
    logger.debug("could not set server version on the MCP SDK internals")


@mcp.tool(
    description=(
        "Get the publish time (UTC) of a user's latest post by username. "
        "Example username: 'exampleuser' (no URL, no leading '@'). " + _ACCOUNT_DOC
    )
)
def get_user_latest_post_time(username: str, account: str | None = None) -> PostTimeResult:
    return get_services(account).post.get_latest_post_time(username)


@mcp.tool(
    description=(
        "List a user's recent posts (newest first), including text, UTC "
        "publish time and permalink. " + _ACCOUNT_DOC
    )
)
def get_user_recent_posts(username: str, limit: int = 10, account: str | None = None) -> list[Post]:
    return get_services(account).post.get_recent_posts(username, limit)


@mcp.tool(
    description=(
        "Search Facebook people by keyword, e.g. 'pest control'. Requires a "
        "logged-in session. " + _ACCOUNT_DOC
    )
)
def search_people(q: str, limit: int = 10, account: str | None = None) -> list[SearchResult]:
    return get_services(account).search.search_people(q, limit)


@mcp.tool(
    description=(
        "Search Facebook pages by keyword, e.g. 'pest control'. Requires a "
        "logged-in session. " + _ACCOUNT_DOC
    )
)
def search_pages(q: str, limit: int = 10, account: str | None = None) -> list[SearchResult]:
    return get_services(account).search.search_pages(q, limit)


@mcp.tool(
    description=(
        "Send a Messenger message to a user or page. 'recipient' accepts a "
        "numeric uid, a username/vanity, or a profile URL. Requires a "
        "logged-in browser (FB_TRANSPORT_MODE=auto or cdp). " + _ACCOUNT_DOC
    )
)
def send_message(recipient: str, text: str, account: str | None = None) -> SendResult:
    return get_services(account).message.send_message(recipient, text)


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
