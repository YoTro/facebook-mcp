"""Findings from the logged-out (incognito) test matrix.

These encode the behaviour measured in a **fresh Chrome instance with zero
Facebook cookies** (``--incognito`` + empty ``--user-data-dir``):

| Scenario | Result |
|---|---|
| ``GET /<username>`` with ``Accept: text/html`` | 200, full ServerJS payload + timeline |
| ``GET /<username>`` **without** ``Accept`` | 200 but a reduced 509 KB page, **no timeline** |
| ``GET /search/{people,pages,top}?q=`` | **404** |
| ``POST /api/graphql/`` search, anonymous | 200 with ``{"message":"Rate limit exceeded","code":1675004}`` |

So: only search needs credentials; the profile path needs a correct ``Accept``
header rather than cookies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from facebook_mcp.clients.base import BaseClient
from facebook_mcp.clients.profile_client import ProfileClient
from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import AuthError, RateLimitError
from facebook_mcp.core.models import SearchCategory
from facebook_mcp.routes.base import Route
from facebook_mcp.session.manager import SessionManager
from facebook_mcp.transport.base import HttpResponse

FIXTURE = Path(__file__).parent / "fixtures" / "profile_page.html"


class _Transport:
    def __init__(self, *, text: str = "", status: int = 200, payload: dict | None = None) -> None:
        self.text = text
        self.status = status
        self.payload = payload or {}
        self.requests: list = []

    @property
    def name(self) -> str:
        return "stub"

    def request(self, req) -> HttpResponse:
        self.requests.append(req)
        if self.payload:
            self.text = json.dumps(self.payload)
        return HttpResponse(
            status_code=self.status,
            url=req.url,
            headers={},
            text=self.text,
            content=b"",
        )

    def close(self) -> None:
        pass


def _session(*, authenticated: bool = False) -> SessionManager:
    cookie = "c_user=1; xs=2" if authenticated else None
    manager = SessionManager(Settings(cookie=cookie, cookie_file=None))
    if authenticated:
        manager.state.fb_dtsg = "DTSG"
        manager.state.lsd = "LSD"
    return manager


# --- profile path: needs headers, not cookies ---------------------------


def test_profile_parses_without_any_cookie() -> None:
    """A logged-out client still gets the full timeline (Accept is what matters)."""
    transport = _Transport(text=FIXTURE.read_text(encoding="utf-8"))
    client = ProfileClient(transport, _session(authenticated=False), Settings())  # type: ignore[arg-type]

    timeline = client.fetch_timeline("exampleuser")
    assert timeline.payload_complete is True
    assert len(timeline.posts) == 2
    assert timeline.latest_post is not None


def test_profile_requests_html_accept_header() -> None:
    transport = _Transport(text=FIXTURE.read_text(encoding="utf-8"))
    client = ProfileClient(transport, _session(), Settings())  # type: ignore[arg-type]
    client.fetch_timeline("exampleuser")

    accept = transport.requests[0].headers.get("Accept", "")
    assert "text/html" in accept, (
        "Facebook serves a reduced page without an HTML Accept header; "
        "the timeline silently disappears"
    )


def test_reduced_page_is_flagged_not_silently_empty() -> None:
    """The 'lite' variant (no timeline section) must be distinguishable."""
    reduced = (
        '<html><body><script type="application/json" data-sjs>'
        '{"require":[["x",[],{"userID":"100000000000001","userVanity":"exampleuser"}]]}'
        "</script></body></html>"
    )
    transport = _Transport(text=reduced)
    client = ProfileClient(transport, _session(), Settings())  # type: ignore[arg-type]

    timeline = client.fetch_timeline("exampleuser")
    assert timeline.posts == []
    assert timeline.payload_complete is False


def test_service_mentions_reduced_page_in_error() -> None:
    from facebook_mcp.services.post_service import PostService

    reduced = (
        '<html><body><script type="application/json" data-sjs>'
        '{"require":[["x",[],{"userID":"100000000000001","userVanity":"exampleuser"}]]}'
        "</script></body></html>"
    )
    transport = _Transport(text=reduced)
    client = ProfileClient(transport, _session(), Settings())  # type: ignore[arg-type]

    result = PostService(client).get_latest_post_time("exampleuser")
    assert result.ok is False
    assert "without a timeline payload" in (result.error or "")


def test_really_empty_timeline_is_not_flagged() -> None:
    """A genuine empty timeline still contains the key, with no edges."""
    page = (
        '<html><body><script type="application/json" data-sjs>'
        '{"require":[["x",[],{"userID":"100000000000001","userVanity":"exampleuser",'
        '"timeline_list_feed_units":{"edges":[]}}]]}'
        "</script></body></html>"
    )
    timeline = ProfileClient(_Transport(text=page), _session(), Settings()).fetch_timeline(  # type: ignore[arg-type]
        "exampleuser"
    )
    assert timeline.posts == []
    assert timeline.payload_complete is True


# --- search path: genuinely needs credentials ---------------------------


def test_search_requires_login_before_any_request() -> None:
    """The client refuses early instead of eating a 404 from Facebook."""
    from facebook_mcp.clients.search_client import SearchClient

    transport = _Transport(payload={"data": {}})
    client = SearchClient(transport, _session(authenticated=False), Settings())  # type: ignore[arg-type]

    with pytest.raises(AuthError):
        client.search("pest control", SearchCategory.PAGES, 3)
    assert transport.requests == []


@pytest.mark.parametrize("code", [1675004])
def test_anonymous_rate_limit_is_reported_as_auth_error(code: int) -> None:
    """Facebook hides 'not logged in' behind a generic rate-limit error."""
    transport = _Transport(
        payload={
            "errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL", "code": code}],
            "extensions": {"is_final": True},
        }
    )
    session = _session(authenticated=False)
    # Bypass require_auth to exercise the error translation itself.
    client = BaseClient(transport, session, Settings())  # type: ignore[arg-type]

    with pytest.raises(AuthError, match="requires a logged-in session"):
        client._raise_for_graphql_errors(
            {"errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL", "code": code}]},
            "search",
        )


def test_authenticated_rate_limit_is_a_rate_limit_error() -> None:
    transport = _Transport()
    client = BaseClient(transport, _session(authenticated=True), Settings())  # type: ignore[arg-type]

    with pytest.raises(RateLimitError):
        client._raise_for_graphql_errors(
            {"errors": [{"message": "Rate limit exceeded", "code": 1675004}]},
            "search",
        )


def test_generic_errors_stay_parse_errors() -> None:
    from facebook_mcp.core.exceptions import ParseError

    client = BaseClient(_Transport(), _session(authenticated=True), Settings())  # type: ignore[arg-type]
    with pytest.raises(ParseError):
        client._raise_for_graphql_errors(
            {"errors": [{"message": "Something odd happened"}]}, "search"
        )


# --- regression guard ---------------------------------------------------


def test_default_headers_include_html_accept() -> None:
    """Guard against a refactor silently breaking the profile scraper."""
    from facebook_mcp.clients.base import _DEFAULT_HEADERS

    assert "text/html" in _DEFAULT_HEADERS["Accept"]
    assert _DEFAULT_HEADERS.get("Accept-Language")


def test_graphql_route_declares_required_auth() -> None:
    from facebook_mcp.routes import graphql

    assert isinstance(graphql.SEARCH_RESULTS, Route)
    assert graphql.SEARCH_RESULTS.requires_auth is True
