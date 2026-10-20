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

    def url(self, **params: str) -> str:
        """Render the route path, substituting ``{placeholders}``."""
        return FACEBOOK_BASE + self.path.format(**params)
