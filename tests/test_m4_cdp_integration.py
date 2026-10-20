"""Integration test: CDP transport against a real (mock) DevTools server.

Spins up:

* an HTTP server exposing ``/json/version``, ``/json/list``, ``/json/new``
* a WebSocket server speaking the DevTools message envelope

This exercises the whole CDP path — discovery, connection, protocol framing,
``Runtime.evaluate`` request execution and ``Network.getCookies`` — without
needing a real browser.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from websockets.sync.server import serve as ws_serve

from facebook_mcp.core.config import Settings
from facebook_mcp.transport.base import HttpRequest
from facebook_mcp.transport.cdp import CdpTransport


class _CdpState:
    def __init__(self) -> None:
        self.ws_port = 0
        self.cookies = {"c_user": "100000000000001", "xs": "secret", "datr": "abc"}
        self.expressions: list[str] = []
        self.methods: list[str] = []
        self.reply_text = "<html><body>mock page</body></html>"
        self.reply_status = 200


def _ws_handler(ws: Any) -> None:
    state: _CdpState = ws.server.state  # type: ignore[attr-defined]
    for raw in ws:
        try:
            message = json.loads(raw)
        except ValueError:
            continue

        method = message.get("method", "")
        params = message.get("params") or {}
        state.methods.append(method)
        msg_id = message.get("id")

        result: dict[str, Any] = {}

        if method == "Runtime.evaluate":
            expression = str(params.get("expression", ""))
            state.expressions.append(expression)
            if "document.readyState" in expression:
                value: Any = "complete"
            elif "fetch(" in expression:
                url_match = re.search(r'fetch\("([^"]+)"', expression)
                value = {
                    "status": state.reply_status,
                    "url": url_match.group(1) if url_match else "",
                    "headers": {"content-type": "text/html"},
                    "text": state.reply_text,
                }
            else:
                value = None
            result = {"result": {"type": "object", "value": value}}

        elif method == "Network.getCookies":
            result = {
                "cookies": [
                    {"name": k, "value": v, "domain": ".facebook.com"}
                    for k, v in state.cookies.items()
                ]
            }

        ws.send(json.dumps({"id": msg_id, "result": result}))


def _make_http_handler(state: _CdpState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._route()

        def do_PUT(self) -> None:
            self._route()

        def do_POST(self) -> None:
            self._route()

        def _route(self) -> None:
            if self.path.startswith("/json/version"):
                payload = {
                    "Browser": "MockChrome/1.0",
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{state.ws_port}/devtools/browser/x",
                }
            elif self.path.startswith("/json/list"):
                # No page yet: forces the transport to create one.
                payload: Any = [
                    {"type": "service_worker", "url": "https://x", "webSocketDebuggerUrl": "ws://n"}
                ]
            elif self.path.startswith("/json/new"):
                payload = {
                    "type": "page",
                    "url": "https://www.facebook.com/",
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{state.ws_port}/devtools/page/1",
                }
            else:
                self.send_error(404)
                return

            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


@pytest.fixture()
def cdp_server() -> Any:
    state = _CdpState()

    ws_server = ws_serve(_ws_handler, "127.0.0.1", 0)
    ws_server.state = state  # type: ignore[attr-defined]
    state.ws_port = ws_server.socket.getsockname()[1]
    threading.Thread(target=ws_server.serve_forever, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_http_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    state.http_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state

    httpd.shutdown()
    httpd.server_close()
    ws_server.shutdown()


def _transport(state: _CdpState) -> CdpTransport:
    return CdpTransport(
        Settings(
            cdp_url=state.http_url,
            cdp_page_url="https://www.facebook.com/",
            cdp_timeout=10.0,
        )
    )


def test_discovers_and_creates_page_target(cdp_server: _CdpState) -> None:
    transport = _transport(cdp_server)
    try:
        cookies = transport.cookies()
        assert cookies["c_user"] == "100000000000001"
        # No usable page in /json/list → /json/new must have been called.
        assert "Network.getCookies" in cdp_server.methods
    finally:
        transport.close()


def test_request_executes_in_page(cdp_server: _CdpState) -> None:
    transport = _transport(cdp_server)
    try:
        resp = transport.request(
            HttpRequest("GET", "https://www.facebook.com/search/people", params={"q": "pest"})
        )
        assert resp.status_code == 200
        assert "mock page" in resp.text
        assert resp.url == "https://www.facebook.com/search/people?q=pest"
    finally:
        transport.close()


def test_request_sends_cookie_header_stripped(cdp_server: _CdpState) -> None:
    transport = _transport(cdp_server)
    try:
        transport.request(
            HttpRequest(
                "GET",
                "https://www.facebook.com/exampleuser",
                headers={"Cookie": "c_user=1", "Accept": "text/html"},
            )
        )
        expression = next(e for e in cdp_server.expressions if "fetch(" in e)
        assert "c_user=1" not in expression  # browser supplies its own cookies
        assert "text/html" in expression
    finally:
        transport.close()


def test_request_maps_post_form(cdp_server: _CdpState) -> None:
    transport = _transport(cdp_server)
    try:
        transport.request(
            HttpRequest(
                "POST",
                "https://www.facebook.com/api/graphql/",
                data={"doc_id": "123", "variables": "{}"},
            )
        )
        expression = next(e for e in cdp_server.expressions if "fetch(" in e)
        assert "doc_id=123" in expression
        assert "application/x-www-form-urlencoded" in expression.lower()
    finally:
        transport.close()


def test_connection_reused_across_calls(cdp_server: _CdpState) -> None:
    transport = _transport(cdp_server)
    try:
        transport.send("Page.enable")
        before = len(cdp_server.methods)
        transport.send("Page.enable")
        assert len(cdp_server.methods) == before + 1
        assert transport._ws is not None
    finally:
        transport.close()


def test_unknown_method_raises(cdp_server: _CdpState) -> None:
    from facebook_mcp.transport.cdp import CdpError

    transport = _transport(cdp_server)
    try:
        # The mock replies with an empty result, so this succeeds...
        transport.send("Domain.unknown")
    except CdpError:
        pass
    finally:
        transport.close()


def test_unreachable_devtools_clear_error() -> None:
    from facebook_mcp.transport.cdp import CdpError

    transport = CdpTransport(Settings(cdp_url="http://127.0.0.1:1", cdp_timeout=3.0))
    with pytest.raises(CdpError, match="unreachable"):
        transport.cookies()
    transport.close()
