"""Transport selection (L1)."""

from __future__ import annotations

from ..core.config import Settings, get_settings
from .base import HttpRequest, HttpResponse, Transport
from .cdp import CdpTransport
from .http import CurlCffiTransport


class HybridTransport(Transport):
    """Routes each request to the transport best suited for it.

    * ``transport_hint == "cdp"`` → live browser (carries login state)
    * everything else             → curl_cffi (fast, no browser needed)

    Both sides are created lazily so an unused transport never connects.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http: CurlCffiTransport | None = None
        self._cdp: CdpTransport | None = None

    @property
    def name(self) -> str:
        return "hybrid"

    @property
    def http(self) -> CurlCffiTransport:
        if self._http is None:
            self._http = CurlCffiTransport(self._settings)
        return self._http

    @property
    def cdp(self) -> CdpTransport:
        if self._cdp is None:
            self._cdp = CdpTransport(self._settings)
        return self._cdp

    def request(self, req: HttpRequest) -> HttpResponse:
        if req.transport_hint == "cdp":
            return self.cdp.request(req)
        return self.http.request(req)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        if self._cdp is not None:
            self._cdp.close()


def get_transport(settings: Settings | None = None) -> Transport:
    """Return the transport selected by ``FB_TRANSPORT_MODE``.

    * ``http`` → curl_cffi only
    * ``cdp``  → Chrome DevTools only
    * ``auto`` → :class:`HybridTransport` (per-request routing)
    """
    settings = settings or get_settings()

    if settings.transport_mode == "cdp":
        return CdpTransport(settings)
    if settings.transport_mode == "auto":
        return HybridTransport(settings)
    return CurlCffiTransport(settings)
