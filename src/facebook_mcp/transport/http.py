"""curl_cffi HTTP transport (L1)."""

from __future__ import annotations

import time
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests

from ..core.config import Settings
from ..core.exceptions import TransportError
from ..core.logger import get_logger
from .base import HttpRequest, HttpResponse, Transport
from .proxy import proxy_mapping, resolve_proxy

logger = get_logger(__name__)

# Transient statuses worth retrying at the transport level.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class CurlCffiTransport(Transport):
    """HTTP transport using curl_cffi with browser TLS/JA3 impersonation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._impersonate = settings.impersonate
        self._timeout = settings.request_timeout
        self._max_retries = settings.max_retries
        self._min_interval = settings.min_request_interval
        self._last_request_started = 0.0

        headers: dict[str, str] = {"Accept-Language": "en-US,en;q=0.9"}
        if settings.user_agent:
            headers["User-Agent"] = settings.user_agent
        self._session = curl_requests.Session(impersonate=self._impersonate, headers=headers)

        self._proxy = resolve_proxy(settings.proxy, settings.proxy_from_system)
        if self._proxy:
            self._session.proxies = proxy_mapping(self._proxy)
            logger.info("using proxy %s", self._proxy)

    @property
    def name(self) -> str:
        return "curl_cffi"

    def request(self, req: HttpRequest) -> HttpResponse:
        self._throttle()
        impersonate = req.impersonate or self._impersonate
        last_error: str | None = None

        for attempt in range(self._max_retries):
            try:
                resp = self._session.request(
                    req.method,
                    req.url,
                    headers=req.headers,
                    params=req.params,
                    data=req.data,
                    json=req.json,
                    impersonate=impersonate,
                    timeout=self._timeout,
                    proxies=self._proxies_for(req.url),
                )
            except Exception as exc:  # noqa: BLE001 - curl_cffi raises various types
                last_error = str(exc)
                self._backoff(req.url, attempt, last_error)
                continue

            status = resp.status_code
            if status not in RETRYABLE_STATUSES or attempt >= self._max_retries - 1:
                return HttpResponse(
                    status_code=status,
                    url=str(resp.url),
                    headers=dict(resp.headers),
                    text=resp.text,
                    content=resp.content,
                )

            last_error = f"HTTP {status}"
            self._backoff(req.url, attempt, last_error, self._retry_after_seconds(resp))

        raise TransportError(
            f"request to {req.url} failed after {self._max_retries} attempts: {last_error}"
        )

    def close(self) -> None:
        self._session.close()

    def _proxies_for(self, url: str) -> dict[str, str] | None:
        """Per-request proxy override.

        Returns ``None`` to use the session default. Loopback targets get an
        empty proxy so local servers / Chrome CDP are never tunnelled.
        """
        if not self._proxy:
            return None
        host = (urlparse(url).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".localhost"):
            return {"http": "", "https": ""}
        return None

    def _backoff(
        self,
        url: str,
        attempt: int,
        reason: str,
        retry_after: float | None = None,
    ) -> None:
        if attempt >= self._max_retries - 1:
            return  # last attempt already consumed, no point sleeping
        wait = retry_after if retry_after is not None else float(2**attempt)
        logger.warning(
            "request to %s %s (attempt %d/%d); retrying in %.1fs",
            url,
            reason,
            attempt + 1,
            self._max_retries,
            wait,
        )
        time.sleep(wait)

    @staticmethod
    def _retry_after_seconds(resp: object) -> float | None:
        header = getattr(resp, "headers", {}).get("Retry-After")
        if not header:
            return None
        header = str(header).strip()
        if header.isdigit():
            return float(header)
        return None  # HTTP-date form: fall back to exponential backoff

    def _throttle(self) -> None:
        """Enforce a minimum interval between request starts."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_started
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_started = time.monotonic()
