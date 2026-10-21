"""GraphQL ``doc_id`` overrides: the rotating value lives in the account store.

``routes/graphql.py`` only carries a bootstrap default — Facebook rotates
``doc_id`` with every frontend build, so a re-capture writes the live value to
``account.json`` and no source change (or commit) is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from facebook_mcp.clients.base import BaseClient
from facebook_mcp.clients.search_client import SearchClient
from facebook_mcp.core.accounts import AccountPaths
from facebook_mcp.core.config import Settings
from facebook_mcp.core.models import SearchCategory
from facebook_mcp.routes import graphql
from facebook_mcp.services.factory import build_settings
from facebook_mcp.session.manager import SessionManager
from facebook_mcp.transport.base import HttpResponse

EMPTY_PAYLOAD: dict[str, Any] = {"data": {"serpResponse": {"results": {"edges": []}}}}


class _Recorder:
    """Transport double that keeps the last request and answers with a payload."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or EMPTY_PAYLOAD
        self.requests: list[Any] = []

    @property
    def name(self) -> str:
        return "recorder"

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


def _session(settings: Settings) -> SessionManager:
    session = SessionManager(settings)
    session.state.fb_dtsg = "DTSGTOKEN"
    session.state.lsd = "LSDTOKEN"
    return session


def _sniff():
    """Import ``tools/sniff.py`` (its CDP import pulls in ``websocket``)."""
    import sys

    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import sniff

    return sniff


# --- resolution order ----------------------------------------------------


def test_override_wins_over_registry() -> None:
    transport = _Recorder()
    settings = Settings(route_doc_ids={"search.results": "555"})
    client = BaseClient(transport, _session(settings), settings)  # type: ignore[arg-type]
    assert client._doc_id(graphql.SEARCH_RESULTS) == "555"


def test_blank_override_falls_back_to_registry() -> None:
    transport = _Recorder()
    settings = Settings(route_doc_ids={"search.results": ""})
    client = BaseClient(transport, _session(settings), settings)  # type: ignore[arg-type]
    assert client._doc_id(graphql.SEARCH_RESULTS) == graphql.SEARCH_RESULTS.doc_id


def test_missing_settings_and_unknown_op_use_registry() -> None:
    transport = _Recorder()
    settings = Settings()
    no_settings = BaseClient(transport, _session(settings), None)  # type: ignore[arg-type]
    assert no_settings._doc_id(graphql.SEARCH_RESULTS) == graphql.SEARCH_RESULTS.doc_id

    other = BaseClient(transport, _session(settings), settings)  # type: ignore[arg-type]
    assert other._doc_id(graphql.SEARCH_RESULTS) == graphql.SEARCH_RESULTS.doc_id


# --- account.json wiring -------------------------------------------------


def test_doc_ids_loaded_from_account_meta() -> None:
    AccountPaths.for_account("default").merge_meta(doc_ids={"search.results": "777"})
    assert build_settings("default").route_doc_ids == {"search.results": "777"}


def test_malformed_doc_ids_are_dropped() -> None:
    AccountPaths.for_account("default").merge_meta(
        doc_ids={
            "search.results": " 888 ",
            "profile.header": "",
            "messaging.send": 42,
            "oops": None,
        }
    )
    assert build_settings("default").route_doc_ids == {"search.results": "888"}


def test_non_dict_doc_ids_is_ignored() -> None:
    AccountPaths.for_account("default").merge_meta(doc_ids=["search.results"])
    assert build_settings("default").route_doc_ids == {}


def test_override_reaches_the_graphql_form() -> None:
    transport = _Recorder()
    settings = Settings(cookie="c_user=42; xs=tok", route_doc_ids={"search.results": "1234567"})
    client = SearchClient(transport, _session(settings), settings)  # type: ignore[arg-type]
    client.search("pest control", SearchCategory.PAGES, 2)

    assert transport.requests[0].data["doc_id"] == "1234567"
    # Everything else about the form is unchanged.
    assert transport.requests[0].data["fb_api_req_friendly_name"] == (
        graphql.SEARCH_RESULTS.friendly_name
    )


# --- capture -> account store -------------------------------------------


def test_capture_records_known_routes_only() -> None:
    sniff = _sniff()
    captured = {
        "a": {"friendly_name": "SearchCometResultsPaginatedResultsQuery", "doc_id": "111"},
        "b": {"friendly_name": "SomeUnrelatedQuery", "doc_id": "222"},
        "c": {"friendly_name": None, "doc_id": "333"},
    }

    meta = sniff._record_doc_ids(Settings(), captured)

    assert meta == AccountPaths.for_account("default").meta_file
    assert json.loads(meta.read_text(encoding="utf-8"))["doc_ids"] == {"search.results": "111"}


def test_capture_merges_with_previous_doc_ids() -> None:
    sniff = _sniff()
    AccountPaths.for_account("default").merge_meta(doc_ids={"profile.header": "999"})

    sniff._record_doc_ids(
        Settings(),
        {"a": {"friendly_name": "SearchCometResultsPaginatedResultsQuery", "doc_id": "444"}},
    )

    meta = json.loads(AccountPaths.for_account("default").meta_file.read_text(encoding="utf-8"))
    assert meta["doc_ids"] == {"profile.header": "999", "search.results": "444"}


def test_capture_without_known_routes_writes_nothing() -> None:
    sniff = _sniff()
    captured = {"a": {"friendly_name": "Nope", "doc_id": "1"}}
    assert sniff._record_doc_ids(Settings(), captured) is None
    assert not AccountPaths.for_account("default").meta_file.exists()
