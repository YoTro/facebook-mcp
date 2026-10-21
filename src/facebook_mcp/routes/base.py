"""Route definition (L2)."""

from __future__ import annotations

from dataclasses import dataclass

FACEBOOK_BASE = "https://www.facebook.com"
GRAPHQL_PATH = "/api/graphql/"


@dataclass(frozen=True)
class Route:
    """A declarative endpoint description.

    Routes are *data*: when Facebook ships a new frontend build, only this
    registry changes, never the clients or services.
    """

    op: str
    """Semantic operation, e.g. ``profile.page`` or ``search.results``."""

    method: str = "GET"
    path: str = "/"
    doc_id: str | None = None
    """GraphQL ``doc_id`` / ``queryID`` (varies per frontend build)."""
    friendly_name: str | None = None
    """GraphQL ``fb_api_req_friendly_name``."""
    requires_auth: bool = False
    anonymous: bool = False
    """Never send session cookies for this route.

    Facebook serves the profile page as a logged-in SPA shell when cookies are
    present, and only the *logged-out* variant embeds the ServerJS payload
    (``timeline_list_feed_units``) the scraper reads. Such routes must be
    fetched without credentials even when a session is configured.
    """

    def url(self, **params: str) -> str:
        """Render the route path, substituting ``{placeholders}``."""
        return FACEBOOK_BASE + self.path.format(**params)
