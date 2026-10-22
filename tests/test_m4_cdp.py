"""M4 tests: CDP transport plumbing, hybrid routing, browser cookie bootstrap."""

from __future__ import annotations

import json

import pytest

from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import AuthError, NotFoundError
from facebook_mcp.session.manager import SessionManager
from facebook_mcp.transport.base import HttpRequest, HttpResponse
from facebook_mcp.transport.cdp import CdpTransport, _fetch_expression
from facebook_mcp.transport.factory import HybridTransport, get_transport


class FakeWs:
    """Records sent commands and replays scripted replies (plus junk events)."""

    def __init__(self, replies: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self.replies = replies or []
        self.closed = False
        self._pending: list[str] = []

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        reply = {"id": message["id"], "result": {"ok": True}}
        if self.replies:
            reply = self.replies.pop(0)
        # Interleave an unrelated event to prove events are skipped.
        self._pending.append(json.dumps({"method": "Network.loadingFinished", "params": {}}))
        self._pending.append(json.dumps(reply))

    def recv(self) -> str:
        return self._pending.pop(0)

    def close(self) -> None:
        self.closed = True


def _transport(fake: FakeWs) -> CdpTransport:
    t = CdpTransport(Settings(), ws_factory=lambda url: fake)
    t._ws = fake  # bypass discovery/connection
    t._ws_url = "ws://fake"
    return t


# --- protocol plumbing ----------------------------------------------------


def test_send_skips_events() -> None:
    fake = FakeWs([{"id": 1, "result": {"cookies": []}}])
    transport = _transport(fake)
    assert transport.send("Network.getCookies", {"urls": ["x"]}) == {"cookies": []}
    assert fake.sent[0]["method"] == "Network.getCookies"
    assert fake.sent[0]["id"] == 1


def test_send_raises_on_protocol_error() -> None:
    from facebook_mcp.transport.cdp import CdpError

    fake = FakeWs([{"id": 1, "error": {"code": -32601, "message": "not found"}}])
    transport = _transport(fake)
    with pytest.raises(CdpError, match="not found"):
        transport.send("Bogus.method")


def test_cookies_parsed() -> None:
    fake = FakeWs(
        [
            {
                "id": 1,
                "result": {
                    "cookies": [
                        {"name": "c_user", "value": "42"},
                        {"name": "xs", "value": "tok"},
                    ]
                },
            }
        ]
    )
    assert _transport(fake).cookies() == {"c_user": "42", "xs": "tok"}


def test_evaluate_raises_on_exception_details() -> None:
    from facebook_mcp.transport.cdp import CdpError

    fake = FakeWs(
        [
            {
                "id": 1,
                "result": {
                    "result": {"type": "object"},
                    "exceptionDetails": {"text": "ReferenceError", "exception": {}},
                },
            }
        ]
    )
    with pytest.raises(CdpError, match="ReferenceError"):
        _transport(fake).evaluate("boom()")


def test_close_closes_socket() -> None:
    fake = FakeWs()
    transport = _transport(fake)
    transport.close()
    assert fake.closed is True
    assert transport._ws is None


# --- request execution ---------------------------------------------------


def test_request_maps_fetch_result() -> None:
    fake = FakeWs(
        [
            {
                "id": 1,
                "result": {
                    "result": {
                        "type": "object",
                        "value": {
                            "status": 200,
                            "url": "https://www.facebook.com/x",
                            "headers": {"content-type": "text/html"},
                            "text": "<html>ok</html>",
                        },
                    }
                },
            }
        ]
    )
    resp = _transport(fake).request(HttpRequest("GET", "https://www.facebook.com/x"))
    assert isinstance(resp, HttpResponse)
    assert resp.status_code == 200
    assert resp.text == "<html>ok</html>"
    assert resp.ok


def test_request_translates_404() -> None:
    fake = FakeWs(
        [
            {
                "id": 1,
                "result": {
                    "result": {
                        "type": "object",
                        "value": {"status": 404, "url": "u", "headers": {}, "text": ""},
                    }
                },
            }
        ]
    )
    with pytest.raises(NotFoundError):
        _transport(fake).request(HttpRequest("GET", "https://www.facebook.com/nope"))


def test_request_reports_fetch_error() -> None:
    from facebook_mcp.transport.cdp import CdpError

    fake = FakeWs(
        [
            {
                "id": 1,
                "result": {
                    "result": {
                        "type": "object",
                        "value": {
                            "status": 0,
                            "url": "u",
                            "headers": {},
                            "text": "",
                            "error": "CORS",
                        },
                    }
                },
            }
        ]
    )
    with pytest.raises(CdpError, match="CORS"):
        _transport(fake).request(HttpRequest("GET", "https://www.facebook.com/x"))


# --- fetch expression building ------------------------------------------


def test_fetch_expression_strips_forbidden_headers() -> None:
    expr = _fetch_expression(
        HttpRequest(
            "GET",
            "https://www.facebook.com/search/people",
            headers={"Cookie": "c_user=1", "User-Agent": "x", "Accept": "text/html"},
            params={"q": "pest control"},
        )
    )
    assert "q=pest+control" in expr
    assert "c_user=1" not in expr
    assert "credentials" in expr
    assert "text/html" in expr


def test_fetch_expression_form_body() -> None:
    expr = _fetch_expression(
        HttpRequest("POST", "https://www.facebook.com/api/graphql/", data={"doc_id": "1"})
    )
    assert "x-www-form-urlencoded" in expr.lower()
    assert "doc_id=1" in expr


def test_fetch_expression_json_body() -> None:
    expr = _fetch_expression(
        HttpRequest("POST", "https://www.facebook.com/api/graphql/", json={"a": 1})
    )
    assert "application/json" in expr
    assert '{\\"a\\": 1}' in expr or '{"a": 1}' in expr


# --- factory routing -----------------------------------------------------


def test_factory_modes() -> None:
    assert isinstance(get_transport(Settings(transport_mode="http")), object)
    assert type(get_transport(Settings(transport_mode="cdp"))).__name__ == "CdpTransport"
    hybrid = get_transport(Settings(transport_mode="auto"))
    assert isinstance(hybrid, HybridTransport)


def test_hybrid_routes_by_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    hybrid = HybridTransport(Settings())
    calls: list[str] = []

    class FakeHttp:
        def request(self, req: HttpRequest) -> HttpResponse:
            calls.append("http")
            return HttpResponse(200, req.url, {}, "", b"")

        def close(self) -> None:
            pass

    class FakeCdp:
        def request(self, req: HttpRequest) -> HttpResponse:
            calls.append("cdp")
            return HttpResponse(200, req.url, {}, "", b"")

        def close(self) -> None:
            pass

    hybrid._http = FakeHttp()  # type: ignore[assignment]
    hybrid._cdp = FakeCdp()  # type: ignore[assignment]

    hybrid.request(HttpRequest("GET", "https://x/", transport_hint=None))
    hybrid.request(HttpRequest("GET", "https://x/", transport_hint="cdp"))
    hybrid.request(HttpRequest("GET", "https://x/", transport_hint="http"))

    assert calls == ["http", "cdp", "http"]


# --- session bootstrap ---------------------------------------------------


def test_session_bootstraps_from_browser() -> None:
    provider_calls = 0

    def provider() -> dict[str, str]:
        nonlocal provider_calls
        provider_calls += 1
        return {"c_user": "42", "xs": "tok", "datr": "d"}

    manager = SessionManager(Settings(cookie=None, cookie_file=None), provider)
    assert manager.is_authenticated is False
    state = manager.require_auth()
    assert state.user_id == "42"
    assert manager.is_authenticated is True
    assert provider_calls == 1


def test_session_bootstrap_without_login_fails() -> None:
    manager = SessionManager(Settings(cookie=None, cookie_file=None), lambda: {"datr": "only-anon"})
    with pytest.raises(AuthError, match="requires a logged-in session"):
        manager.require_auth()


def test_session_bootstrap_provider_error_is_soft() -> None:
    def boom() -> dict[str, str]:
        raise RuntimeError("chrome not running")

    manager = SessionManager(Settings(cookie=None, cookie_file=None), boom)
    assert manager.bootstrap_from_browser() is False
    with pytest.raises(AuthError):
        manager.require_auth()


def test_session_without_provider_raises() -> None:
    manager = SessionManager(Settings(cookie=None, cookie_file=None))
    with pytest.raises(AuthError):
        manager.require_auth()


# --- composition root ----------------------------------------------------


def test_services_wires_cookie_provider() -> None:
    from facebook_mcp.services.factory import Services

    services = Services(Settings(transport_mode="auto", cookie=None, cookie_file=None))
    try:
        assert isinstance(services.transport, HybridTransport)
        assert services.session._cookie_provider is not None
    finally:
        services.close()


# --- anonymous routes must omit browser credentials ---------------------
#
# fetch() attaches the browser cookie jar regardless of headers, so a route that
# must look logged-out needs credentials: "omit" rather than a missing header.


def test_fetch_expression_respects_omit_credentials() -> None:
    expr = _fetch_expression(HttpRequest("GET", "https://www.facebook.com/x"))
    assert '"credentials": "include"' in expr


def test_fetch_expression_can_omit_credentials() -> None:
    expr = _fetch_expression(HttpRequest("GET", "https://www.facebook.com/x", credentials="omit"))
    assert '"credentials": "omit"' in expr


def test_anonymous_route_omits_credentials_end_to_end() -> None:
    """PROFILE_PAGE is anonymous, so the CDP request must omit credentials."""
    from facebook_mcp.clients.profile_client import ProfileClient
    from facebook_mcp.session.manager import SessionManager

    class _Recorder:
        def __init__(self) -> None:
            self.requests: list[HttpRequest] = []

        @property
        def name(self) -> str:
            return "recorder"

        def request(self, req: HttpRequest) -> HttpResponse:
            self.requests.append(req)
            return HttpResponse(200, req.url, {}, "<html></html>", b"")

        def close(self) -> None:
            pass

    transport = _Recorder()
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = ProfileClient(transport, session, Settings())  # type: ignore[arg-type]
    try:
        client.fetch_timeline("exampleuser")
    except ValueError:
        pass  # the stub body has no profile payload; the request is what matters

    assert transport.requests[-1].credentials == "omit"
    assert "Cookie" not in transport.requests[-1].headers


# --- transport-raised AuthError must invalidate the session -------------
#
# The CDP transport maps the in-page status itself and raises AuthError from
# inside request(), so BaseClient's post-return status check never runs.


class _AuthRaisingTransport:
    def __init__(self) -> None:
        self.requests = 0

    @property
    def name(self) -> str:
        return "auth-raising"

    def request(self, req: HttpRequest) -> HttpResponse:
        self.requests += 1
        from facebook_mcp.core.exceptions import AuthError

        raise AuthError("access denied (HTTP 403)")

    def close(self) -> None:
        pass


def test_transport_auth_error_invalidates_the_session() -> None:
    from facebook_mcp.clients.base import BaseClient
    from facebook_mcp.core.exceptions import AuthError
    from facebook_mcp.routes.base import Route
    from facebook_mcp.session.manager import SessionManager

    transport = _AuthRaisingTransport()
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = BaseClient(transport, session, Settings())  # type: ignore[arg-type]

    with pytest.raises(AuthError):
        client._request(Route(op="x", method="GET", path="/x", requires_auth=True))

    assert session.rejected, "a rejected browser session kept claiming to be valid"
    with pytest.raises(AuthError, match="rejected"):
        session.require_auth()


def test_transport_auth_error_on_a_public_route_keeps_the_session() -> None:
    from facebook_mcp.clients.base import BaseClient
    from facebook_mcp.core.exceptions import AuthError
    from facebook_mcp.routes.base import Route
    from facebook_mcp.session.manager import SessionManager

    transport = _AuthRaisingTransport()
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = BaseClient(transport, session, Settings())  # type: ignore[arg-type]

    with pytest.raises(AuthError):
        client._request(Route(op="x", method="GET", path="/x", requires_auth=False))

    assert session.rejected is None


def test_drain_events_honours_the_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A continuous event stream must not keep a capture alive past its budget."""
    import time

    from facebook_mcp.transport.cdp import CdpTransport

    class _ChattyWs:
        timeout = 1.0

        def __init__(self) -> None:
            self.calls = 0

        def settimeout(self, value: float) -> None:
            self.timeout = value

        def recv(self) -> str:
            self.calls += 1
            return json.dumps({"method": "Network.requestWillBeSent", "params": {}})

    ws = _ChattyWs()
    transport = CdpTransport(Settings())
    transport._ws = ws  # type: ignore[assignment]
    transport._ws_url = "ws://fake"

    started = time.monotonic()
    events = transport.drain_events(timeout=0.5, deadline=time.monotonic() + 0.30)
    elapsed = time.monotonic() - started

    assert events, "buffered/streamed events should still be returned"
    assert elapsed < 2.0, f"drain_events ignored the deadline ({elapsed:.2f}s)"
    assert ws.calls > 0
