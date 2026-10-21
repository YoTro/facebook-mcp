"""REST / HTML routes (L2).

Reconnaissance notes (2026-10, logged-out):

* ``GET /<username>``                      → 200, embeds ServerJS JSON with
  ``userID`` / ``userVanity`` / ``timeline_list_feed_units``.
* ``GET /search/people?q=...``             → 404 when logged out (needs auth).
* ``GET /search/pages?q=...``              → 404 when logged out (needs auth).
"""

from __future__ import annotations

from .base import Route

PROFILE_PAGE = Route(
    op="profile.page",
    method="GET",
    path="/{username}",
    # Logged-in visitors get a client-rendered shell with no timeline payload,
    # so this route is always fetched anonymously (see Route.anonymous).
    anonymous=True,
)

SEARCH_PEOPLE_PAGE = Route(
    op="search.people.page",
    method="GET",
    path="/search/people",
    requires_auth=True,
)

SEARCH_PAGES_PAGE = Route(
    op="search.pages.page",
    method="GET",
    path="/search/pages",
    requires_auth=True,
)

ALL: tuple[Route, ...] = (PROFILE_PAGE, SEARCH_PEOPLE_PAGE, SEARCH_PAGES_PAGE)
