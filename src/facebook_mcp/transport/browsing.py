"""Browser-capable transport helpers (L1)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .base import Transport
from .factory import HybridTransport


@runtime_checkable
class BrowserTransport(Protocol):
    """Structural contract for transports that can drive a live browser."""

    def evaluate(self, expression: str, **kwargs: Any) -> Any: ...

    def navigate(self, url: str, *, timeout: float | None = None) -> None: ...

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


def cdp_from(transport: Transport) -> Any | None:
    """Return a browser-capable transport, or ``None``.

    Recognises both a direct CDP transport and the hybrid transport (whose
    browser half is exposed lazily as ``.cdp``).
    """
    if isinstance(transport, HybridTransport):
        return transport.cdp
    if isinstance(transport, BrowserTransport):
        return transport
    return None
