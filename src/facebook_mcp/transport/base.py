"""Transport protocol and HTTP request/response models (L1)."""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..core.exceptions import HttpStatusError

TransportHint = Literal["http", "cdp"]
Credentials = Literal["include", "omit"]

# Facebook prefixes JSON bodies of its legacy AJAX endpoints with this guard so
# the response cannot be captured as a cross-site ``<script>`` (JSON hijacking).
# GraphQL success bodies are clean, but every error envelope carries it — and a
# rotated ``doc_id`` lands exactly there.
JSON_GUARD_PREFIX = "for (;;);"


@dataclass(frozen=True)
class HttpRequest:
    """A transport-agnostic HTTP request description."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] | None = None  # query-string parameters
    data: str | dict[str, str] | None = None  # form-encoded body (dict or str)
    json: Any | None = None  # JSON body (takes precedence over data)
    impersonate: str | None = None  # per-request TLS fingerprint override
    transport_hint: TransportHint | None = None
    """Preferred transport; ``cdp`` routes through the live browser."""
    credentials: Credentials = "include"
    """``omit`` keeps cookies out of a browser-executed request.

    Browsers attach their own cookie jar to ``fetch()`` regardless of headers,
    so an anonymous route needs ``credentials: "omit"`` rather than just a
    missing ``Cookie`` header."""


@dataclass(frozen=True)
class HttpResponse:
    """A transport-agnostic HTTP response."""

    status_code: int
    url: str
    headers: dict[str, str]
    text: str
    content: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        """Decode the body as JSON, tolerating Facebook's anti-hijacking guard."""
        text = self.text.lstrip()
        if text.startswith(JSON_GUARD_PREFIX):
            text = text[len(JSON_GUARD_PREFIX) :]
        return _json.loads(text)

    def raise_for_status(self) -> HttpResponse:
        if not self.ok:
            raise HttpStatusError(
                f"HTTP {self.status_code} for {self.url}", status_code=self.status_code
            )
        return self


@runtime_checkable
class Transport(Protocol):
    """Minimal transport contract shared by all implementations.

    Implementations:
    * :class:`~facebook_mcp.transport.http.CurlCffiTransport` (M1)
    * :class:`~facebook_mcp.transport.cdp.CdpTransport` (M4)
    """

    @property
    def name(self) -> str: ...

    def request(self, req: HttpRequest) -> HttpResponse: ...

    def close(self) -> None: ...
