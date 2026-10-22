"""Chrome DevTools Protocol transport (L1).

Talks to a Chrome instance launched with ``--remote-debugging-port=9222``.
Requests are executed **inside the page** via ``Runtime.evaluate`` + ``fetch``,
so the browser supplies its own cookies, ``User-Agent``, ``sec-*`` headers and
TLS fingerprint — nothing has to be exported or hardcoded.

This transport is used for:

* authenticated operations (search, messaging)
* bootstrapping a cookie jar for the pure-HTTP transport
* packet capture / ``doc_id`` discovery (see ``tools/sniff.py``)
"""

from __future__ import annotations

import contextlib
import json
import time
from collections import deque
from typing import Any
from urllib.parse import urlencode

import websocket

from ..core.config import Settings
from ..core.exceptions import (
    AuthError,
    HttpStatusError,
    NotFoundError,
    RateLimitError,
    TransportError,
)
from ..core.logger import get_logger
from .base import HttpRequest, HttpResponse, Transport
from .throttle import limiter_for

logger = get_logger(__name__)

# Headers the browser refuses to let script code override.
_FORBIDDEN_HEADERS = {
    "cookie",
    "user-agent",
    "host",
    "content-length",
    "connection",
    "origin",
    "referer",
    "accept-encoding",
}

_DEFAULT_PAGE_URL = "https://www.facebook.com/"


class CdpError(TransportError):
    """A DevTools protocol call failed."""

    code = "cdp_error"


class CdpTransport(Transport):
    """Executes HTTP requests inside a live, possibly logged-in Chrome."""

    def __init__(self, settings: Settings, *, ws_factory: Any | None = None) -> None:
        self._settings = settings
        self._base = settings.cdp_url.rstrip("/")
        self._page_url = settings.cdp_page_url or _DEFAULT_PAGE_URL
        self._timeout = settings.cdp_timeout
        self._ws: Any | None = None
        self._ws_url: str | None = None
        self._next_id = 1
        self._ws_factory = ws_factory or self._default_ws_factory
        # Events seen while waiting for a command reply are buffered here so
        # nothing is lost for callers that care about them (packet capture).
        self._events: deque[dict[str, Any]] = deque(maxlen=10_000)
        # Same per-account budget as the HTTP transport: a GraphQL call run
        # inside the browser hits the same limits as one sent with curl_cffi.
        self._limiter = limiter_for(settings)

    @property
    def name(self) -> str:
        return "cdp"

    # --- public API -----------------------------------------------------

    def request(self, req: HttpRequest) -> HttpResponse:
        """Perform ``req`` from inside the page and return the response."""
        self._limiter.wait()
        result = self.evaluate(_fetch_expression(req), await_promise=True)
        if not isinstance(result, dict):
            raise CdpError("unexpected fetch result from page")

        status = int(result.get("status") or 0)
        text = result.get("text") or ""
        if result.get("error"):
            raise CdpError(f"in-page fetch failed for {req.url}: {result['error']}")

        resp = HttpResponse(
            status_code=status,
            url=result.get("url") or req.url,
            headers=result.get("headers") or {},
            text=text,
            content=text.encode("utf-8", errors="replace"),
        )
        _raise_for_status(resp)
        return resp

    def cookies(self, url: str | None = None) -> dict[str, str]:
        """Return cookies for ``url`` (defaults to the facebook origin)."""
        target = url or self._page_url
        payload = self._send("Network.getCookies", {"urls": [target]})
        cookies = payload.get("cookies") or []
        return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}

    def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = False,
        return_by_value: bool = True,
    ) -> Any:
        """Run ``expression`` in the page's main world and return its value."""
        payload = self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
                "userGesture": True,
            },
        )

        if "exceptionDetails" in payload:
            details = payload["exceptionDetails"]
            message = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "javascript exception"
            )
            raise CdpError(f"evaluate failed: {message}")

        result = payload.get("result") or {}
        if result.get("subtype") == "error":
            raise CdpError(f"evaluate returned an error: {result.get('description')}")
        return result.get("value")

    def navigate(self, url: str, *, timeout: float | None = None) -> None:
        """Navigate the attached page and wait for the load event."""
        self._send("Page.enable")
        self._send("Page.navigate", {"url": url})
        self._wait_for_load(timeout or self._timeout)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Expose raw protocol calls (used by ``tools/sniff.py``)."""
        return self._send(method, params or {})

    def drain_events(self, timeout: float = 1.0) -> list[dict[str, Any]]:
        """Return buffered events, reading more from the socket until ``timeout``.

        Used by packet capture; protocol replies are unaffected because
        :meth:`_send` always consumes its own reply first.
        """
        ws = self._connect()
        buffered = list(self._events)
        self._events.clear()

        previous_timeout = getattr(ws, "timeout", None)
        try:
            ws.settimeout(timeout)
            while True:
                try:
                    raw = ws.recv()
                except Exception:  # noqa: BLE001 - timeout is the normal exit
                    break
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "method" in message:
                    buffered.append(message)
        finally:
            if previous_timeout is not None:
                with contextlib.suppress(Exception):
                    ws.settimeout(previous_timeout)
        return buffered

    def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._ws.close()
            self._ws = None
            self._ws_url = None

    # --- connection -----------------------------------------------------

    def _default_ws_factory(self, ws_url: str) -> Any:
        # Chrome rejects DevTools WebSocket handshakes that carry an Origin
        # header unless --remote-allow-origins is set. Suppressing it keeps
        # Chrome's default security posture intact.
        return websocket.create_connection(ws_url, timeout=self._timeout, suppress_origin=True)

    def _connect(self) -> Any:
        if self._ws is not None:
            return self._ws

        ws_url = self._page_ws_url()
        try:
            self._ws = self._ws_factory(ws_url)
        except Exception as exc:
            raise CdpError(
                f"cannot connect to Chrome at {ws_url}: {exc}. "
                "Start Chrome with --remote-debugging-port=9222"
            ) from exc

        self._ws_url = ws_url
        logger.info("attached to Chrome page %s", ws_url.split("/")[-1][:12])
        return self._ws

    def _http_json(self, path: str, method: str = "GET") -> Any:
        """Small JSON-over-HTTP helper for the DevTools discovery endpoints."""
        from curl_cffi import requests as curl_requests

        url = f"{self._base}{path}"
        try:
            resp = curl_requests.request(method, url, timeout=5, proxies={"http": "", "https": ""})
        except Exception as exc:
            raise CdpError(
                f"Chrome DevTools endpoint {url} unreachable ({exc}). "
                "Start Chrome with --remote-debugging-port=9222"
            ) from exc
        if resp.status_code >= 400:
            raise CdpError(f"DevTools endpoint {url} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise CdpError(f"DevTools endpoint {url} returned non-JSON") from exc

    def _page_ws_url(self) -> str:
        if self._ws_url:
            return self._ws_url

        targets = self._http_json("/json/list")
        page = self._pick_target(targets)
        if page is None:
            page = self._new_target(self._page_url)
        ws_url = page.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CdpError("Chrome target has no webSocketDebuggerUrl")
        return str(ws_url)

    def _pick_target(self, targets: Any) -> dict[str, Any] | None:
        if not isinstance(targets, list):
            return None
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for target in pages:
            if "facebook.com" in (target.get("url") or ""):
                return target
        return pages[0] if pages else None

    def _new_target(self, url: str) -> dict[str, Any]:
        # Chrome >= 111 requires PUT; older builds accept GET.
        for method in ("PUT", "POST", "GET"):
            try:
                return self._http_json(f"/json/new?{urlencode({'': ''})[1:]}{url}", method)
            except CdpError:
                continue
        raise CdpError("could not create a Chrome page target")

    def _wait_for_load(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.evaluate("document.readyState")
            if state == "complete":
                return
            time.sleep(0.2)
        logger.warning("page did not reach readyState=complete within %.1fs", timeout)

    # --- protocol plumbing ----------------------------------------------

    def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command, skipping interleaved events, and return its result."""
        ws = self._connect()
        message_id = self._next_id
        self._next_id += 1

        try:
            ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        except Exception as exc:
            self.close()
            raise CdpError(f"failed to send {method}: {exc}") from exc

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except Exception as exc:
                self.close()
                raise CdpError(f"failed to receive reply for {method}: {exc}") from exc

            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue

            if message.get("id") != message_id:
                if "method" in message:
                    self._events.append(message)
                continue  # an event, not our reply

            if "error" in message:
                error = message["error"]
                raise CdpError(f"{method} failed: {error.get('message')} ({error.get('code')})")
            return message.get("result") or {}

        raise CdpError(f"timed out after {self._timeout}s waiting for {method}")


# --- helpers --------------------------------------------------------------


def _fetch_expression(req: HttpRequest) -> str:
    """Build a page-side fetch() expression for ``req``."""
    url = req.url
    if req.params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(req.params)}"

    headers = {k: v for k, v in (req.headers or {}).items() if k.lower() not in _FORBIDDEN_HEADERS}

    body: str | None = None
    if req.json is not None:
        body = json.dumps(req.json)
        headers.setdefault("Content-Type", "application/json")
    elif isinstance(req.data, dict):
        body = urlencode(req.data)
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(req.data, str):
        body = req.data

    init: dict[str, Any] = {
        "method": req.method.upper(),
        "headers": headers,
        "credentials": req.credentials,
        "redirect": "follow",
    }
    if body is not None:
        init["body"] = body

    return f"""
(async () => {{
  try {{
    const res = await fetch({json.dumps(url)}, {json.dumps(init)});
    const text = await res.text();
    const headers = {{}};
    res.headers.forEach((v, k) => {{ headers[k] = v; }});
    return {{ status: res.status, url: res.url, headers, text }};
  }} catch (e) {{
    return {{ status: 0, url: {json.dumps(url)}, headers: {{}}, text: "", error: String(e) }};
  }}
}})()
"""


def _raise_for_status(resp: HttpResponse) -> None:
    if resp.status_code == 429:
        raise RateLimitError(f"rate limited on {resp.url}")
    if resp.status_code == 404:
        raise NotFoundError(f"not found: {resp.url}")
    if resp.status_code in (401, 403):
        raise AuthError(f"access denied (HTTP {resp.status_code}): {resp.url}")
    if resp.status_code >= 400:
        raise HttpStatusError(
            f"HTTP {resp.status_code} for {resp.url}", status_code=resp.status_code
        )
