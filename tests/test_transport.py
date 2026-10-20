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
        if self.path == "/ok":
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
