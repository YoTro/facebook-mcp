"""M1 transport layer tests against a local HTTP server."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import HttpStatusError, TransportError
from facebook_mcp.transport import CurlCffiTransport, HttpRequest


class _Handler(BaseHTTPRequestHandler):
    flaky_count = 0

    def do_GET(self) -> None:
        if self.path == "/echo-headers":
            self._respond(200, str(dict(self.headers)))
        elif self.path == "/set-cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "c_user=99; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/ok":
            self._respond(200, "hello")
        elif self.path == "/status/404":
            self._respond(404, "not found")
        elif self.path == "/flaky":
            type(self).flaky_count += 1
            if type(self).flaky_count == 1:
                self._respond(500, "boom")
            else:
                self._respond(200, "recovered")
        elif self.path == "/json":
            self._respond(200, json.dumps({"a": 1}), content_type="application/json")
        else:
            self._respond(404, "unknown")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        self._respond(200, f"{self.command} {self.path} {body}")

    def _respond(self, code: int, body: str, content_type: str = "text/plain") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:  # silence request logs
        pass


@pytest.fixture()
def server() -> str:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    _Handler.flaky_count = 0
    yield base
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def transport() -> CurlCffiTransport:
    t = CurlCffiTransport(
        Settings(
            impersonate="chrome",
            min_request_interval=0.0,
            max_retries=1,
            request_timeout=10.0,
        )
    )
    yield t
    t.close()


def _make_transport(**kwargs: object) -> CurlCffiTransport:
    defaults = dict(
        impersonate="chrome",
        min_request_interval=0.0,
        max_retries=1,
        request_timeout=10.0,
    )
    defaults.update(kwargs)
    return CurlCffiTransport(Settings(**defaults))


def test_get_ok(transport: CurlCffiTransport, server: str) -> None:
    resp = transport.request(HttpRequest("GET", f"{server}/ok"))
    assert resp.status_code == 200
    assert resp.text == "hello"
    assert resp.ok


def test_response_json(transport: CurlCffiTransport, server: str) -> None:
    resp = transport.request(HttpRequest("GET", f"{server}/json"))
    assert resp.json() == {"a": 1}


def test_post_form_data(transport: CurlCffiTransport, server: str) -> None:
    resp = transport.request(HttpRequest("POST", f"{server}/echo", data={"k": "v"}))
    assert resp.status_code == 200
    assert "k=v" in resp.text


def test_raise_for_status(transport: CurlCffiTransport, server: str) -> None:
    resp = transport.request(HttpRequest("GET", f"{server}/status/404"))
    with pytest.raises(HttpStatusError):
        resp.raise_for_status()


def test_retry_then_succeed(server: str) -> None:
    t = _make_transport(max_retries=2)
    try:
        resp = t.request(HttpRequest("GET", f"{server}/flaky"))
        assert resp.status_code == 200
        assert resp.text == "recovered"
    finally:
        t.close()


def test_transport_error_after_retries() -> None:
    t = _make_transport(max_retries=1)
    try:
        with pytest.raises(TransportError):
            t.request(HttpRequest("GET", "http://127.0.0.1:1/"))
    finally:
        t.close()


def test_throttle(server: str) -> None:
    t = _make_transport(min_request_interval=0.2)
    try:
        start = time.monotonic()
        t.request(HttpRequest("GET", f"{server}/ok"))
        t.request(HttpRequest("GET", f"{server}/ok"))
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15
    finally:
        t.close()


def test_throttle_applies_to_every_retry(server: str) -> None:
    """A retry is still a request: it must respect min_request_interval."""
    t = _make_transport(max_retries=2, min_request_interval=0.3)
    # Isolate the throttle from the exponential backoff.
    t._backoff = lambda *args, **kwargs: None  # type: ignore[method-assign]
    try:
        start = time.monotonic()
        resp = t.request(HttpRequest("GET", f"{server}/flaky"))
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        # Two attempts, at least one full interval apart.
        assert elapsed >= 0.25, f"retry bypassed the throttle (elapsed {elapsed:.2f}s)"
    finally:
        t.close()


def test_omit_credentials_never_sends_the_jar(server: str) -> None:
    """A response can set cookies; an "omit" request must still be cookie-free.

    curl_cffi applies the session jar independently of the Cookie header, so a
    GraphQL response that sets c_user/xs would otherwise turn the "anonymous"
    profile fetch back into a logged-in one.
    """
    t = _make_transport(max_retries=1)
    try:
        # /json does not set cookies, so seed the jar explicitly.
        t._session.cookies.set("c_user", "42", domain="127.0.0.1")

        resp = t.request(HttpRequest("GET", f"{server}/echo-headers", credentials="omit"))
        assert "Cookie" not in resp.text

        # The same request without "omit" does carry it.
        resp = t.request(HttpRequest("GET", f"{server}/echo-headers"))
        assert "c_user=42" in resp.text
    finally:
        t.close()


def test_omit_requests_do_not_pollute_each_other(server: str) -> None:
    """The anonymous session is cleared before every omit request."""
    t = _make_transport(max_retries=1)
    try:
        t.request(HttpRequest("GET", f"{server}/set-cookie", credentials="omit"))
        resp = t.request(HttpRequest("GET", f"{server}/echo-headers", credentials="omit"))
        assert "Cookie" not in resp.text
    finally:
        t.close()


# --- shared pacing -------------------------------------------------------


def test_limiter_is_shared_by_every_channel_of_an_account() -> None:
    from facebook_mcp.transport.throttle import limiter_for

    settings = Settings(account="work", min_request_interval=0.2)
    same = limiter_for(settings.model_copy())
    assert limiter_for(settings) is same

    other = Settings(account="other", min_request_interval=0.2)
    assert limiter_for(other) is not same


def test_cdp_requests_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """GraphQL executed inside the browser must respect the same interval."""
    from facebook_mcp.transport.cdp import CdpTransport

    transport = CdpTransport(Settings(min_request_interval=0.2))
    calls: list[str] = []

    def fake_evaluate(expression: str, **kwargs: object) -> dict[str, object]:
        calls.append(expression)
        return {"status": 200, "text": "ok", "url": "https://www.facebook.com/x"}

    monkeypatch.setattr(transport, "evaluate", fake_evaluate)

    start = time.monotonic()
    transport.request(HttpRequest("GET", "https://www.facebook.com/x"))
    transport.request(HttpRequest("GET", "https://www.facebook.com/x"))
    elapsed = time.monotonic() - start

    assert len(calls) == 2
    assert elapsed >= 0.15, f"CDP bypassed the limiter (elapsed {elapsed:.2f}s)"


def test_message_ui_operations_are_paced() -> None:
    """Navigating and sending draw from the account's limiter too."""
    from facebook_mcp.clients.message_client import MessageClient
    from facebook_mcp.session.manager import SessionManager

    waits: list[int] = []
    client = MessageClient(_NoopTransport(), SessionManager(Settings()), Settings())
    client._limiter = type("L", (), {"wait": lambda self: waits.append(1)})()  # type: ignore[assignment]

    thread_id, _target = client._open_thread(_FakeCdp(), "1234567890")
    assert thread_id == "1234567890"
    assert waits, "UI navigation did not consult the limiter"


class _NoopTransport:
    name = "noop"

    def request(self, req):  # type: ignore[no-untyped-def]
        raise AssertionError("not used")

    def close(self) -> None:
        pass


class _FakeCdp:
    name = "fake-cdp"

    def navigate(self, url: str, timeout: float | None = None) -> None:
        self.url = url

    def evaluate(self, expression: str, **kwargs: object) -> object:
        if "location.href" in expression:
            return self.url
        if "return !!composer" in expression:
            return True
        return None

    def cookies(self, url: str | None = None) -> dict[str, str]:
        return {}
