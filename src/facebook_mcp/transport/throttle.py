"""Shared request pacing (L1).

``min_request_interval`` has to cover every channel, not just curl_cffi: a
GraphQL call executed inside the browser and the UI operations that send a
message hit the same account and the same anti-abuse limits. One limiter per
account gives all of them a single budget, so mixing channels cannot double the
request rate.
"""

from __future__ import annotations

import threading
import time

from ..core.config import Settings


class RateLimiter:
    """Enforce a minimum interval between the starts of two operations."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._last = 0.0
        self._lock = threading.Lock()

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def wait(self) -> None:
        """Sleep as needed so the previous operation is at least N seconds old."""
        if self._min_interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


_limiters: dict[tuple[str, float], RateLimiter] = {}
_registry_lock = threading.Lock()


def limiter_for(settings: Settings) -> RateLimiter:
    """Return the process-wide limiter shared by every channel of ``settings``."""
    key = (settings.account, float(settings.min_request_interval))
    with _registry_lock:
        limiter = _limiters.get(key)
        if limiter is None:
            limiter = _limiters[key] = RateLimiter(settings.min_request_interval)
        return limiter


def reset_limiters() -> None:
    """Drop every limiter. Used by tests so pacing never leaks between them."""
    with _registry_lock:
        _limiters.clear()
