"""Service composition root (L4).

Builds the object graph once: settings → transport → session → clients →
services. The MCP layer only ever sees the resulting services.

Multi-account: one :class:`Services` instance per account. ``get_services()``
caches them by account name, so tools can switch accounts per call without
paying for re-initialisation.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from ..clients.message_client import MessageClient
from ..clients.profile_client import ProfileClient
from ..clients.search_client import SearchClient
from ..core.accounts import AccountPaths
from ..core.config import Settings, get_settings
from ..session.manager import SessionManager
from ..transport.base import Transport
from ..transport.cdp import CdpTransport
from ..transport.factory import HybridTransport, get_transport
from .message_service import MessageService
from .post_service import PostService
from .search_service import SearchService


def build_settings(account: str | None = None) -> Settings:
    """Return settings for ``account``, applying per-account overrides.

    ``account.json`` may carry ``cdp_url`` so each account can bind to its own
    Chrome instance (separate profile + port) for browser-backed operations.
    """
    base = get_settings()
    if account and account != base.account:
        base = base.model_copy(update={"account": account})

    paths = AccountPaths.for_account(base.account, base.home)
    meta = paths.load_meta()
    overrides: dict[str, object] = {}
    for key in ("cdp_url", "cdp_page_url", "user_agent", "impersonate"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            overrides[key] = value
    if overrides:
        base = base.model_copy(update=overrides)
    return base


class Services:
    """Container holding every service, sharing one transport and session."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.transport: Transport = get_transport(self.settings)
        self.session = SessionManager(self.settings, self._cookie_provider())

        self.profile_client = ProfileClient(self.transport, self.session, self.settings)
        self.search_client = SearchClient(self.transport, self.session, self.settings)
        self.message_client = MessageClient(
            self.transport, self.session, self.settings, self.profile_client
        )

        self.post = PostService(self.profile_client)
        self.search = SearchService(self.search_client)
        self.message = MessageService(self.message_client)

    @property
    def account(self) -> str:
        return self.settings.account

    def close(self) -> None:
        self.transport.close()

    def _cookie_provider(self) -> Callable[[], dict[str, str]] | None:
        """Expose the browser's cookie jar to the session layer, if reachable."""
        if not self.settings.cdp_cookie_bootstrap:
            return None
        transport = self.transport
        if isinstance(transport, HybridTransport):
            return transport.cdp.cookies
        if isinstance(transport, CdpTransport):
            return transport.cookies
        return None


@lru_cache(maxsize=16)
def _services_for(account_key: str) -> Services:
    return Services(build_settings(account_key or None))


def get_services(account: str | None = None) -> Services:
    """Return the :class:`Services` container for ``account``.

    ``account=None`` uses ``FB_ACCOUNT`` (default ``"default"``). Instances are
    cached per account, so repeated calls are cheap.
    """
    key = account or get_settings().account
    return _services_for(key)
