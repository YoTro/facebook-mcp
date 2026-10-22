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
from ..core.accounts import AccountPaths, resolve_home
from ..core.config import Settings, get_settings
from ..core.logger import get_logger
from ..session.manager import SessionManager
from ..transport.base import Transport
from ..transport.cdp import CdpTransport
from ..transport.factory import HybridTransport, get_transport
from .message_service import MessageService
from .post_service import PostService
from .search_service import SearchService

logger = get_logger(__name__)


def build_settings(account: str | None = None, *, home: str | None = None) -> Settings:
    """Return settings for ``account``, applying per-account overrides.

    ``account.json`` may carry ``cdp_url`` so each account can bind to its own
    Chrome instance (separate profile + port) for browser-backed operations,
    and ``doc_ids`` so a re-captured GraphQL ``doc_id`` takes effect without
    editing ``routes/graphql.py``.

    ``home`` overrides the credential root (``tools/* --home``); without it the
    global ``FB_HOME`` applies.
    """
    base = get_settings()
    if home:
        base = base.model_copy(update={"home": str(resolve_home(home))})

    if account and account != base.account:
        # Everything global describes the identity of FB_ACCOUNT, not of every
        # account. Cookies would make account="work" act as whoever owns the
        # global jar; cdp_url would make it *borrow the other account's browser*
        # and bootstrap that browser's cookies through the CDP provider. Both are
        # dropped; an account may only use a browser bound to itself.
        if base.cookie or base.cookie_file:
            logger.warning(
                "account %r: ignoring the global FB_COOKIE / FB_COOKIE_FILE "
                "(they belong to account %r); use its own account store instead",
                account,
                base.account,
            )
        base = base.model_copy(
            update={
                "account": account,
                "cookie": None,
                "cookie_file": None,
                "cdp_url": None,
            }
        )

    paths = AccountPaths.for_account(base.account, base.home)
    meta = paths.load_meta()
    overrides: dict[str, object] = {}
    for key in ("cdp_url", "cdp_page_url", "user_agent", "impersonate"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            overrides[key] = value
    doc_ids = _clean_doc_ids(meta.get("doc_ids"))
    if doc_ids:
        overrides["route_doc_ids"] = doc_ids
    if overrides:
        base = base.model_copy(update=overrides)
    return base


def _clean_doc_ids(raw: object) -> dict[str, str]:
    """Keep only ``{op: non-empty str}`` entries from ``account.json``.

    ``model_copy`` does not validate, so anything malformed is dropped here
    rather than reaching :meth:`BaseClient._graphql` as a non-string.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        str(op): value.strip()
        for op, value in raw.items()
        if isinstance(value, str) and value.strip()
    }


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
