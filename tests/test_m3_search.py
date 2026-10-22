"""M3 tests: search variable templating, response parsing and GraphQL plumbing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from facebook_mcp.clients.base import BaseClient
from facebook_mcp.clients.search_client import SearchClient
from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import ParseError
from facebook_mcp.core.models import SearchCategory
from facebook_mcp.routes import graphql
from facebook_mcp.routes.templates import (
    TemplateError,
    build_search_variables,
    load_template,
)
from facebook_mcp.server import mcp
from facebook_mcp.session.manager import SessionManager
from facebook_mcp.transport.base import HttpResponse
from facebook_mcp.transport.factory import HybridTransport

FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


@pytest.fixture(scope="module")
def payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- variable templates --------------------------------------------------


def test_search_template_present() -> None:
    template = load_template(
        __import__(
            "facebook_mcp.routes.templates", fromlist=["SEARCH_RESULTS_TEMPLATE"]
        ).SEARCH_RESULTS_TEMPLATE
    )
    assert isinstance(template, dict)
    assert "args" in template


def test_build_search_variables_substitutes() -> None:
    variables = build_search_variables("pest control", "pages", 7)
    assert variables["args"]["text"] == "pest control"
    assert variables["args"]["experience"]["type"] == "PAGES_TAB"
    assert variables["count"] == 7
    assert "flow_cursors_serialized" in variables["cursor"]


def test_build_search_variables_people_tab() -> None:
    variables = build_search_variables("x", "people", 3)
    assert variables["args"]["experience"]["type"] == "PEOPLE_TAB"


def test_build_search_variables_rotates_bsid() -> None:
    a = build_search_variables("x", "pages", 3)["args"]["context"]["bsid"]
    b = build_search_variables("x", "pages", 3)["args"]["context"]["bsid"]
    assert a != b


def test_build_search_variables_clamps_count() -> None:
    assert build_search_variables("x", "pages", 999)["count"] == 50
    assert build_search_variables("x", "pages", 0)["count"] == 1


def test_build_search_variables_rejects_unknown_category() -> None:
    with pytest.raises(TemplateError):
        build_search_variables("x", "groups", 5)


def test_template_does_not_leak_between_calls() -> None:
    first = build_search_variables("alpha", "pages", 3)
    second = build_search_variables("beta", "people", 3)
    assert first["args"]["text"] == "alpha"
    assert second["args"]["text"] == "beta"
    # The on-disk template must stay pristine.
    reloaded = build_search_variables("gamma", "pages", 3)
    assert reloaded["args"]["text"] == "gamma"


# --- response parsing ----------------------------------------------------


def test_parse_results_pages(payload: dict[str, Any]) -> None:
    results = SearchClient._parse_results(payload, SearchCategory.PAGES)
    # Page-typed nodes are kept; User-typed people are dropped even though
    # Facebook returns them in this tab; duplicates and malformed nodes skipped.
    assert [r.id for r in results] == ["987654321"]


def test_user_typed_page_in_page_search_is_kept() -> None:
    """Some surfaces return real pages as __typename="User" + category fields."""
    payload = {
        "data": {
            "serpResponse": {
                "results": {
                    "edges": [
                        {
                            "rendering_strategy": {
                                "view_model": {
                                    "profile": {
                                        "id": "42",
                                        "name": "Some Page",
                                        "__typename": "User",
                                        "category_name": "Pest Control Service",
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    results = SearchClient._parse_results(payload, SearchCategory.PAGES)
    assert [r.id for r in results] == ["42"]


def test_people_search_keeps_user_typed_entries() -> None:
    payload = {
        "data": {
            "serpResponse": {
                "results": {
                    "edges": [
                        {
                            "rendering_strategy": {
                                "view_model": {
                                    "profile": {
                                        "id": "7",
                                        "name": "A Person",
                                        "__typename": "User",
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    results = SearchClient._parse_results(payload, SearchCategory.PEOPLE)
    assert [r.id for r in results] == ["7"]


def test_parse_results_keeps_pages_in_page_search(payload: dict[str, Any]) -> None:
    results = SearchClient._parse_results(payload, SearchCategory.PAGES)
    page = next(r for r in results if r.id == "987654321")
    assert page.name == "Example Business"
    assert page.url == "https://www.facebook.com/example-business"
    assert page.category is SearchCategory.PAGES
    assert page.verified is True
    assert page.subtitle == "Pest Control Service"
    assert page.avatar_url == "https://scontent.example/fb/987654321.jpg"


def test_parse_results_people_excludes_page_entity(payload: dict[str, Any]) -> None:
    results = SearchClient._parse_results(payload, SearchCategory.PEOPLE)
    assert all(r.id != "987654321" for r in results)
    assert all(r.category is SearchCategory.PEOPLE for r in results)


def test_parse_results_missing_container_raises() -> None:
    with pytest.raises(ParseError, match="shape changed"):
        SearchClient._parse_results({"data": {}}, SearchCategory.PAGES)


def test_next_cursor(payload: dict[str, Any]) -> None:
    client = object.__new__(SearchClient)
    assert client.next_cursor(payload) is not None
    assert client.next_cursor({"data": {}}) is None


# --- GraphQL plumbing ---------------------------------------------------


class _RecordingTransport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[Any] = []

    @property
    def name(self) -> str:
        return "recording"

    def request(self, req: Any) -> HttpResponse:
        self.requests.append(req)
        return HttpResponse(
            status_code=200,
            url=req.url,
            headers={},
            text=json.dumps(self.payload),
            content=b"",
        )

    def close(self) -> None:
        pass


def _search_client(payload: dict[str, Any]) -> tuple[SearchClient, _RecordingTransport]:
    transport = _RecordingTransport(payload)
    session = SessionManager(
        Settings(cookie="c_user=42; xs=tok", cookie_file=None),
    )
    session.state.fb_dtsg = "DTSGTOKEN"
    session.state.lsd = "LSDTOKEN"
    return SearchClient(transport, session, Settings()), transport  # type: ignore[arg-type]


def test_search_posts_expected_form(payload: dict[str, Any]) -> None:
    client, transport = _search_client(payload)
    client.search("pest control", SearchCategory.PAGES, 4)

    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == "POST"
    assert req.url == "https://www.facebook.com/api/graphql/"
    assert req.data["doc_id"] == graphql.SEARCH_RESULTS.doc_id
    assert req.data["fb_api_req_friendly_name"] == "SearchCometResultsPaginatedResultsQuery"
    assert req.data["fb_dtsg"] == "DTSGTOKEN"
    assert req.data["lsd"] == "LSDTOKEN"
    # jazoest = "2" + sum(ord) of fb_dtsg
    assert req.data["jazoest"] == "2" + str(sum(ord(c) for c in "DTSGTOKEN"))
    assert json.loads(req.data["variables"])["args"]["text"] == "pest control"


def test_search_respects_limit(payload: dict[str, Any]) -> None:
    client, _ = _search_client(payload)
    # The people tab keeps both User-typed nodes; the pages tab only has one.
    results = client.search("pest control", SearchCategory.PEOPLE, 2)
    assert len(results) == 2


def test_search_rejects_empty_query(payload: dict[str, Any]) -> None:
    client, _ = _search_client(payload)
    with pytest.raises(ValueError):
        client.search("   ", SearchCategory.PAGES, 5)


def test_graphql_error_translated_to_parse_error() -> None:
    stale = {"errors": [{"message": "A server error missing_required_variable_value"}], "data": {}}
    client, _ = _search_client(stale)
    with pytest.raises(ParseError, match="stale"):
        client.search("pest control", SearchCategory.PAGES, 3)


def test_graphql_auth_error_translated() -> None:
    from facebook_mcp.core.exceptions import AuthError

    denied = {"errors": [{"message": "Please log in to continue"}], "data": {}}
    client, _ = _search_client(denied)
    with pytest.raises(AuthError):
        client.search("pest control", SearchCategory.PAGES, 3)


# --- transport routing (protocol first) ---------------------------------


def test_authenticated_http_when_cookies_configured() -> None:
    """With configured cookies we must stay on curl_cffi, not the browser."""
    transport = _RecordingTransport({})
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = BaseClient(transport, session, Settings())  # type: ignore[arg-type]

    from facebook_mcp.routes.base import Route

    client._request(Route(op="x", method="GET", path="/x", requires_auth=True))
    assert transport.requests[-1].transport_hint is None


def test_authenticated_cdp_when_cookies_come_from_browser() -> None:
    transport = _RecordingTransport({})
    session = SessionManager(
        Settings(cookie=None, cookie_file=None), lambda: {"c_user": "1", "xs": "2"}
    )
    client = BaseClient(transport, session, Settings())  # type: ignore[arg-type]

    from facebook_mcp.routes.base import Route

    client._request(Route(op="x", method="GET", path="/x", requires_auth=True))
    assert transport.requests[-1].transport_hint == "cdp"


# --- MCP wiring ---------------------------------------------------------


def test_search_tools_registered() -> None:
    import asyncio

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"search_people", "search_pages"} <= names


def test_services_exposes_search() -> None:
    from facebook_mcp.services.factory import Services

    services = Services(Settings(transport_mode="auto", cookie="c_user=1; xs=2"))
    try:
        assert services.search is not None
        assert isinstance(services.transport, HybridTransport)
    finally:
        services.close()


def test_graphql_auth_error_invalidates_the_session() -> None:
    """A 'log in' Relay error means the cookie jar is dead; retrying loops."""
    client, _ = _search_client({"errors": [{"message": "Please log in to continue"}]})
    client._session.state.fb_dtsg = "DTSGTOKEN"

    with pytest.raises(Exception, match="log in"):
        client.search("pest control", SearchCategory.PAGES, 2)

    assert client._session.state.fb_dtsg is None
    assert client._session.rejected is not None
