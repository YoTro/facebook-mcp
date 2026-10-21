"""GraphQL ``doc_id`` overrides: the rotating value lives in the account store.

``routes/graphql.py`` only carries a bootstrap default — Facebook rotates
``doc_id`` with every frontend build, so a re-capture writes the live value to
``account.json`` and no source change (or commit) is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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


# --- error envelope for a rotated / wrong doc_id -------------------------
#
# Facebook answers HTTP 200 with a *legacy* envelope when the query id is
# unknown to the build it serves:
#   for (;;);{"__ar":1,"error":1357031,"errorSummary":"This content is no longer available",...}
# Before this was handled the client raised a bare JSONDecodeError, which hid
# the real cause and bypassed the project's exception hierarchy.


class _RawRecorder(_Recorder):
    """Transport double that returns a verbatim body and status."""

    def __init__(self, text: str, status: int = 200) -> None:
        super().__init__()
        self._text = text
        self._status = status

    def request(self, req: Any) -> HttpResponse:
        self.requests.append(req)
        return HttpResponse(
            status_code=self._status,
            url=req.url,
            headers={"content-type": "application/x-javascript"},
            text=self._text,
            content=b"",
        )


LEGACY_ERROR = (
    'for (;;);{"__ar":1,"error":1357031,"errorSummary":"This content is no longer '
    'available","errorDescription":"The content you requested cannot be displayed '
    'right now."}'
)


def _client_with(text: str, status: int = 200) -> BaseClient:
    settings = Settings(cookie="c_user=42; xs=tok", route_doc_ids={"search.results": "777"})
    return BaseClient(_RawRecorder(text, status), _session(settings), settings)  # type: ignore[arg-type]


def test_guard_prefix_is_stripped_by_http_response() -> None:
    from facebook_mcp.transport.base import HttpResponse

    resp = HttpResponse(200, "u", {}, 'for (;;);{"a": 1}', b"")
    assert resp.json() == {"a": 1}


def test_legacy_error_envelope_becomes_parse_error() -> None:
    from facebook_mcp.core.exceptions import ParseError

    client = _client_with(LEGACY_ERROR)
    with pytest.raises(ParseError) as excinfo:
        client._graphql(graphql.SEARCH_RESULTS, {})

    message = str(excinfo.value)
    assert "1357031" in message
    assert "doc_id 777" in message
    assert "tools/sniff.py" in message


def test_search_surfaces_parse_error_not_json_decode_error() -> None:
    from facebook_mcp.clients.search_client import SearchClient
    from facebook_mcp.core.exceptions import ParseError

    settings = Settings(cookie="c_user=42; xs=tok", route_doc_ids={"search.results": "777"})
    client = SearchClient(_RawRecorder(LEGACY_ERROR), _session(settings), settings)

    with pytest.raises(ParseError, match="1357031"):
        client.search("pest control", SearchCategory.PAGES, 2)


def test_unparseable_body_reports_doc_id() -> None:
    from facebook_mcp.core.exceptions import ParseError

    client = _client_with("<html>maintenance</html>")
    with pytest.raises(ParseError, match="was not JSON"):
        client._graphql(graphql.SEARCH_RESULTS, {})


def test_plain_relay_payload_is_untouched() -> None:
    client = _client_with('{"data": {"ok": true}}')
    assert client._graphql(graphql.SEARCH_RESULTS, {}) == {"data": {"ok": True}}


# --- template promotion is driven by an explicit mapping ------------------
#
# PROMOTED_TEMPLATES maps a captured query to the file its runtime consumer
# reads. A hardcoded target would silently overwrite the search template as soon
# as a second query joined the allow-list.


def test_promotion_uses_the_mapped_filename(tmp_path: Path, monkeypatch) -> None:
    sniff = _sniff()
    monkeypatch.setattr(sniff, "PACKAGE_TEMPLATES", tmp_path)
    monkeypatch.setattr(
        sniff,
        "PROMOTED_TEMPLATES",
        {"SearchCometResultsPaginatedResultsQuery": "search_results.json", "Other": "other.json"},
    )

    written = sniff._save_template("Other", {"args": {}, "count": 1})

    assert written == tmp_path / "other.json"
    assert json.loads(written.read_text(encoding="utf-8"))["args"]["text"] == "{query}"
    assert not (tmp_path / "search_results.json").exists()


def test_unpromoted_capture_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    sniff = _sniff()
    monkeypatch.setattr(sniff, "PACKAGE_TEMPLATES", tmp_path)

    assert sniff._save_template("SomeOtherQuery", {"args": {}}) is None
    assert list(tmp_path.iterdir()) == []


def test_relay_unknown_document_is_reported_as_rotated_doc_id() -> None:
    """A valid-looking but unknown id returns a Relay error, not the legacy one."""
    from facebook_mcp.core.exceptions import ParseError

    payload = {
        "errors": [
            {
                "message": "The GraphQL document with ID 1 was not found.",
                "severity": "CRITICAL",
            }
        ]
    }
    client = _client_with(json.dumps(payload))
    with pytest.raises(ParseError, match="probably rotated"):
        client._raise_for_graphql_errors(payload, "search")
