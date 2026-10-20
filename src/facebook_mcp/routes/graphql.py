"""GraphQL ``doc_id`` registry (L2).

Facebook's Relay endpoints are addressed by an opaque ``doc_id`` that changes
with each frontend build. Values below were captured from the live site
(2026-10) — re-capture with ``tools/sniff.py`` when a query starts failing;
only this table changes, never the clients.

The search query additionally needs a large, build-specific ``variables``
object; it is stored as data in ``templates/search_results.json`` and turned
into a request by :func:`facebook_mcp.routes.templates.build_search_variables`.
"""

from __future__ import annotations

from .base import Route

PROFILE_HEADER = Route(
    op="profile.header",
    method="POST",
    path="/api/graphql/",
    doc_id="28585259111117102",
    friendly_name="ProfileCometHeaderQuery",
)

PROFILE_TIMELINE_REFETCH = Route(
    op="profile.timeline.refetch",
    method="POST",
    path="/api/graphql/",
    doc_id=None,  # not present in a logged-out preloader; capture from a timeline
    friendly_name="ProfileCometTimelineFeedRefetchQuery",
)

SEARCH_RESULTS = Route(
    op="search.results",
    method="POST",
    path="/api/graphql/",
    doc_id="28778193718476003",
    friendly_name="SearchCometResultsPaginatedResultsQuery",
    requires_auth=True,
)

SEND_MESSAGE = Route(
    op="messaging.send",
    method="POST",
    path="/api/graphql/",
    doc_id=None,  # capture from a logged-in Messenger session
    friendly_name="SendMessageMutation",
    requires_auth=True,
)

ALL: tuple[Route, ...] = (
    PROFILE_HEADER,
    PROFILE_TIMELINE_REFETCH,
    SEARCH_RESULTS,
    SEND_MESSAGE,
)
