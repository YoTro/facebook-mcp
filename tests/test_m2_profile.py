"""M2 tests: extraction helpers, session, routes and profile client parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from facebook_mcp.clients.profile_client import ProfileClient
from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import AuthError, NotFoundError, RateLimitError
from facebook_mcp.core.extract import dig, find_all, find_first, iter_json_scripts, walk
from facebook_mcp.routes import rest
from facebook_mcp.session import SessionManager, extractor
from facebook_mcp.transport.base import HttpResponse

FIXTURE = Path(__file__).parent / "fixtures" / "profile_page.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# --- core.extract ---------------------------------------------------------


def test_iter_json_scripts_skips_invalid(html: str) -> None:
    payloads = list(iter_json_scripts(html))
    assert len(payloads) == 3  # 4 script tags, one invalid
    assert all(isinstance(p, dict) for p in payloads)


def test_find_first_and_all(html: str) -> None:
    payloads = list(iter_json_scripts(html))
    merged = {"root": payloads}
    units = find_first(merged, "timeline_list_feed_units")
    assert isinstance(units, dict)
    assert len(find_all(merged, "post_id")) == 2


def test_walk_and_dig(html: str) -> None:
    payloads = list(iter_json_scripts(html))
    dicts = [n for n in walk(payloads) if isinstance(n, dict)]
    assert any(n.get("userVanity") == "exampleuser" for n in dicts)
    assert dig({"a": {"b": [{"c": 1}]}}, "a", "b", 0, "c") == 1
    assert dig({"a": 1}, "a", "b") is None
    assert dig({"a": [1]}, "a", 5) is None


# --- session --------------------------------------------------------------


def test_cookie_string_parsing() -> None:
    state = extractor.from_cookie_string("c_user=123; xs=abc%3D; fr=xyz")
    assert state.cookies["c_user"] == "123"
    assert state.is_authenticated is True
    assert "c_user=123" in state.cookie_header()


def test_cookie_string_without_pairs_raises() -> None:
    with pytest.raises(AuthError):
        extractor.from_cookie_string("garbage")


def test_cookie_file_parsing(tmp_path: Path) -> None:
    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t456\n"
        ".facebook.com\tTRUE\t/\tTRUE\t0\txs\ttoken\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tother\tx\n",
        encoding="utf-8",
    )
    state = extractor.from_cookie_file(f)
    assert state.cookies == {"c_user": "456", "xs": "token"}


def test_cookie_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(AuthError):
        extractor.from_cookie_file(tmp_path / "nope.txt")


def test_tokens_from_html() -> None:
    page = '["DTSGInitialData",[],{"token":"DTSG_TOKEN"},258],["LSD",[],{"token":"LSD_TOKEN"},323]'
    state = extractor.tokens_from_html(page)
    assert state.fb_dtsg == "DTSG_TOKEN"
    assert state.lsd == "LSD_TOKEN"


def test_session_manager_anonymous() -> None:
    manager = SessionManager(Settings(cookie=None, cookie_file=None))
    assert manager.is_authenticated is False
    with pytest.raises(AuthError):
        manager.require_auth()


def test_session_manager_learns_tokens() -> None:
    manager = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    assert manager.state.lsd is None
    manager.learn_from_html('["LSD",[],{"token":"T"},323]')
    assert manager.state.lsd == "T"
    assert manager.is_authenticated is True


def test_session_state_merge_does_not_override() -> None:
    from facebook_mcp.session.state import SessionState

    base = SessionState(cookies={"c_user": "1"})
    base.merge_tokens(SessionState(cookies={"c_user": "2", "datr": "d"}, lsd="L"))
    assert base.cookies["c_user"] == "1"
    assert base.cookies["datr"] == "d"
    assert base.lsd == "L"


# --- routes ---------------------------------------------------------------


def test_route_url_rendering() -> None:
    assert rest.PROFILE_PAGE.url(username="exampleuser") == ("https://www.facebook.com/exampleuser")


def test_search_routes_require_auth() -> None:
    assert rest.SEARCH_PEOPLE_PAGE.requires_auth is True
    assert rest.SEARCH_PAGES_PAGE.requires_auth is True


# --- profile client parsing (offline) ------------------------------------


class _FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self._response = response
        self.requests: list[object] = []

    @property
    def name(self) -> str:
        return "fake"

    def request(self, req: object) -> HttpResponse:
        self.requests.append(req)
        return self._response

    def close(self) -> None:
        pass


def _client(html: str) -> ProfileClient:
    response = HttpResponse(
        status_code=200,
        url="https://www.facebook.com/exampleuser",
        headers={},
        text=html,
        content=b"",
    )
    session = SessionManager(Settings(cookie=None, cookie_file=None))
    return ProfileClient(_FakeTransport(response), session, Settings())


def test_parse_timeline(html: str) -> None:
    timeline = _client(html).fetch_timeline("exampleuser")
    assert timeline.profile.uid == "100000000000001"
    assert timeline.profile.username == "exampleuser"
    assert timeline.profile.display_name == "Example Page"
    assert len(timeline.posts) == 2


def test_latest_post_is_newest(html: str) -> None:
    timeline = _client(html).fetch_timeline("exampleuser")
    latest = timeline.latest_post
    assert latest is not None
    assert latest.id == "1111111111111111"
    assert latest.created_at == datetime.fromtimestamp(1790751234, tz=UTC)
    assert latest.text == "first post body"
    assert latest.url is not None and latest.url.endswith("pfbid0AAA")


def test_username_normalised(html: str) -> None:
    client = _client(html)
    timeline = client.fetch_timeline("  @exampleuser ")
    assert timeline.profile.username == "exampleuser"


def test_empty_username_rejected() -> None:
    with pytest.raises(ValueError):
        _client("<html></html>").fetch_timeline("   ")


def test_unresolvable_profile_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _client("<html></html>").fetch_timeline("nobody")


# --- error translation ---------------------------------------------------


def _client_with_status(code: int) -> ProfileClient:
    response = HttpResponse(status_code=code, url="u", headers={}, text="", content=b"")
    return ProfileClient(_FakeTransport(response), SessionManager(Settings()), Settings())


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, NotFoundError), (429, RateLimitError), (403, AuthError)],
)
def test_http_status_translation(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        _client_with_status(status).fetch_timeline("someuser")


# --- profile pages are fetched anonymously on purpose ---------------------
#
# With cookies, Facebook serves the logged-in SPA shell: no userVanity, no
# userID, no timeline_list_feed_units. Only the logged-out variant carries the
# ServerJS payload, so PROFILE_PAGE must never send the session cookie — even
# when the account has one configured (which is the recommended setup).


def test_profile_route_is_marked_anonymous() -> None:
    assert rest.PROFILE_PAGE.anonymous is True
    assert rest.SEARCH_PEOPLE_PAGE.anonymous is False


def test_profile_request_omits_the_cookie_header() -> None:
    transport = _FakeTransport(
        HttpResponse(
            status_code=200,
            url="https://www.facebook.com/exampleuser",
            headers={},
            text=FIXTURE.read_text(encoding="utf-8"),
            content=b"",
        )
    )
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = ProfileClient(transport, session, Settings())

    client.fetch_timeline("exampleuser")

    sent = transport.requests[0]
    assert "Cookie" not in sent.headers


def test_authenticated_routes_still_send_cookies() -> None:
    transport = _FakeTransport(
        HttpResponse(status_code=200, url="u", headers={}, text="{}", content=b"")
    )
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = ProfileClient(transport, session, Settings())

    from facebook_mcp.routes.base import Route

    client._request(Route(op="x", method="GET", path="/x"))
    assert transport.requests[-1].headers.get("Cookie") == "c_user=1; xs=2"


# --- curl's HttpOnly marker -------------------------------------------------


def test_httponly_cookies_are_parsed(tmp_path: Path) -> None:
    """curl writes HttpOnly entries as ``#HttpOnly_<domain>``, not comments."""
    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t456\n"
        "#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t0\txs\ttoken\n"
        "# this one is a real comment\n"
        ".facebook.com\tTRUE\t/\tFALSE\t0\tdatr\td\n",
        encoding="utf-8",
    )
    state = extractor.from_cookie_file(f)
    assert state.cookies["c_user"] == "456"
    assert state.cookies["xs"] == "token"
    assert state.cookies["datr"] == "d"
    assert state.is_authenticated is True
