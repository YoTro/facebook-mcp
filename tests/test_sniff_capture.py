"""The capture loop must read events the CDP client already buffered.

``CdpTransport.__send`` keeps every event it sees while waiting for a command
reply. A capture loop that reads the raw WebSocket skips that buffer, so the
requests fired during ``Page.navigate()`` — exactly the interesting ones — are
lost even though they reached the client.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sniff


def _event(url: str, doc_id: str, friendly: str, variables: str = '{"args":{}}') -> dict:
    body = f"doc_id={doc_id}&fb_api_req_friendly_name={friendly}&variables={variables}"
    return {
        "method": "Network.requestWillBeSent",
        "params": {
            "request": {
                "url": url,
                "method": "POST",
                "postData": body,
                "headers": {"X-FB-Friendly-Name": friendly},
            }
        },
    }


class _FakeTransport:
    """Returns one batch (the "already buffered" events), then nothing."""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = batches
        self.calls = 0

    def drain_events(self, timeout: float = 1.0, deadline: float | None = None) -> list[dict]:
        self.calls += 1
        return self._batches.pop(0) if self._batches else []


def test_buffered_events_are_captured() -> None:
    transport = _FakeTransport(
        [
            [
                _event(
                    "https://www.facebook.com/api/graphql/",
                    "111",
                    "SearchCometResultsPaginatedResultsQuery",
                ),
                _event("https://www.facebook.com/api/graphql/", "222", "SomeOtherQuery"),
                {"method": "Network.loadingFinished", "params": {}},  # ignored
            ]
        ]
    )
    seen: list[dict] = []

    sniff._capture(transport, time.monotonic() + 0.2, "graphql", seen.append)

    assert [entry["doc_id"] for entry in seen] == ["111", "222"]
    assert transport.calls >= 1


def test_non_graphql_urls_are_filtered() -> None:
    transport = _FakeTransport(
        [
            [
                _event("https://static.xx.fbcdn.net/app.js", "1", "Nope"),
                _event("https://www.facebook.com/api/graphql/", "2", "Yes"),
            ]
        ]
    )
    seen: list[dict] = []

    sniff._capture(transport, time.monotonic() + 0.2, "graphql", seen.append)

    assert [entry["doc_id"] for entry in seen] == ["2"]


def test_entry_ignores_requests_without_a_query_identity() -> None:
    event = {
        "method": "Network.requestWillBeSent",
        "params": {"request": {"url": "https://www.facebook.com/api/graphql/", "postData": "x=1"}},
    }
    assert sniff._entry_from_event(event, "graphql") is None


def test_entry_extracts_variables() -> None:
    entry = sniff._entry_from_event(
        _event("https://www.facebook.com/api/graphql/", "9", "Query", '{"text":"a"}'), "graphql"
    )
    assert entry is not None
    assert entry["variables"] == {"text": "a"}


def test_websocket_events_reach_the_recorder(tmp_path) -> None:
    """--websocket must actually be wired into the capture loop."""
    created = {
        "method": "Network.webSocketCreated",
        "params": {"url": "wss://gateway.facebook.com/ws/lightspeed?x=1"},
    }
    frame = {
        "method": "Network.webSocketFrameSent",
        "params": {"response": {"opcode": 2, "payloadData": "AAECAwQFBgcICQ=="}},
    }
    transport = _FakeTransport([[created, frame]])

    seen: list[dict] = []
    recorded: list[dict] = []

    def on_ws(message: dict) -> None:
        recorded.append(message)
        sniff._record_websocket(message, seen, tmp_path)

    sniff._capture(transport, time.monotonic() + 0.2, "graphql", lambda e: None, on_ws)

    assert [m["method"] for m in recorded] == [
        "Network.webSocketCreated",
        "Network.webSocketFrameSent",
    ]
    assert seen[0]["event"] == "open"
    assert "lightspeed" in str(seen[0]["url"])
    written = list((tmp_path).glob("*.bin"))
    assert written, "the binary frame was not saved"
    assert written[0].read_bytes() == bytes([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
