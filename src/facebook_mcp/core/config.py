"""Application configuration (L0 core layer)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables / ``.env``.

    Environment variables use the ``FB_`` prefix (e.g. ``FB_COOKIE``,
    ``FB_TRANSPORT_MODE``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FB_",
        extra="ignore",
    )

    # --- transport -----------------------------------------------------------
    # http: curl_cffi only
    # cdp:  Chrome DevTools only
    # auto: public reads via http, writes / authenticated ops via cdp
    transport_mode: Literal["http", "cdp", "auto"] = "auto"

    # --- http (curl_cffi) ----------------------------------------------------
    impersonate: str = "chrome"  # TLS/JA3 fingerprint to impersonate
    request_timeout: float = 30.0  # seconds
    max_retries: int = 3

    # --- session / auth ------------------------------------------------------
    # Which account directory under FB_HOME/accounts is active.
    account: str = "default"
    # Credential root; empty -> ~/.facebook-mcp (or $FB_HOME).
    home: str | None = None

    cookie: str | None = None  # Netscape-format cookie string
    cookie_file: str | None = None  # explicit path; overrides the account dir
    user_agent: str | None = None  # optional UA override

    # --- cdp -----------------------------------------------------------------
    # ``None`` means "no browser is bound to this account": the global value
    # describes FB_ACCOUNT only, and services/factory.py clears it for any other
    # account that has no cdp_url of its own.
    cdp_url: str | None = "http://127.0.0.1:9222"
    # Page the transport attaches to; requests run inside this origin so the
    # browser's own cookies/headers are used.
    cdp_page_url: str = "https://www.facebook.com/"
    cdp_timeout: float = 60.0
    # When no cookies are configured, pull them from the attached Chrome on
    # first authenticated operation instead of failing immediately.
    cdp_cookie_bootstrap: bool = True

    # --- proxy ---------------------------------------------------------------
    # Explicit proxy URL, e.g. http://127.0.0.1:10808 or socks5h://127.0.0.1:10808
    proxy: str | None = None
    # On macOS, fall back to the system proxy from `scutil --proxy` when
    # `proxy` is unset and no *_proxy env var is present.
    proxy_from_system: bool = True

    # --- messaging -----------------------------------------------------------
    # lightspeed: protocol-level send over the Messenger sync WebSocket
    # ui:         drive the Messenger DOM through CDP (fallback)
    # auto:       lightspeed first, UI when the protocol path cannot be used
    message_transport: Literal["lightspeed", "ui", "auto"] = "auto"

    # --- rate limiting -------------------------------------------------------
    min_request_interval: float = 2.0  # seconds between requests

    # --- graphql route overrides ---------------------------------------------
    # ``doc_id`` rotates with every Facebook frontend build, and two accounts can
    # be served different builds. ``routes/graphql.py`` therefore only holds a
    # bootstrap default; a re-capture (``tools/sniff.py --save-templates``)
    # writes the live value into the account store instead, so refreshing it is
    # never a source change. Maps semantic op -> doc_id, e.g.
    # ``{"search.results": "39411546341825607"}``.
    route_doc_ids: dict[str, str] = Field(default_factory=dict)

    @property
    def has_credentials(self) -> bool:
        """Whether any cookie source is configured."""
        return bool(self.cookie or self.cookie_file)

    def account_paths(self):
        """Filesystem layout for the active account."""
        from .accounts import AccountPaths

        return AccountPaths.for_account(self.account, self.home)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` singleton."""
    return Settings()
