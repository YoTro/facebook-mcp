"""Search client (L3).

Search results are **not** in the initial HTML (they are rendered client-side),
so this client replays Facebook's Relay query directly:

* endpoint : ``POST /api/graphql/``
* doc_id   : ``SearchCometResultsPaginatedResultsQuery``
* variables: captured template + ``args.text`` / ``experience.type``

Verified shape of a result edge::

    data.serpResponse.results.edges[*].rendering_strategy
        .view_model.profile.{id, name, url, profile_picture.uri, is_verified}
"""

from __future__ import annotations

from typing import Any

from ..core.exceptions import ParseError
from ..core.extract import dig
from ..core.logger import get_logger
from ..core.models import SearchCategory, SearchResult
from ..routes import graphql
from ..routes.templates import build_search_variables
from .base import BaseClient

logger = get_logger(__name__)

RESULTS_PATH = ("data", "serpResponse", "results")
PAGE_INFO_PATH = ("data", "serpResponse", "results", "page_info")


class SearchClient(BaseClient):
    """Searches Facebook for people or pages."""

    def search(self, query: str, category: SearchCategory, limit: int = 10) -> list[SearchResult]:
        query = query.strip()
        if not query:
            raise ValueError("search query must not be empty")
        limit = max(1, min(int(limit), 50))

        variables = build_search_variables(query, category.value, limit)
        payload = self._graphql(graphql.SEARCH_RESULTS, variables)
        self._raise_for_graphql_errors(payload, "search")

        results = self._parse_results(payload, category)
        logger.info("search %r (%s) -> %d results", query, category.value, len(results))
        return results[:limit]

    def next_cursor(self, payload: dict[str, Any]) -> str | None:
        """Return the pagination cursor from a raw search payload, if any."""
        page_info = dig(payload, *PAGE_INFO_PATH) or {}
        if isinstance(page_info, dict) and page_info.get("has_next_page"):
            cursor = page_info.get("end_cursor")
            return str(cursor) if cursor else None
        return None

    @staticmethod
    def _parse_results(payload: dict[str, Any], category: SearchCategory) -> list[SearchResult]:
        container = dig(payload, *RESULTS_PATH)
        if not isinstance(container, dict):
            raise ParseError("search response shape changed: data.serpResponse.results missing")

        edges = container.get("edges") or []
        results: list[SearchResult] = []
        seen: set[str] = set()

        for edge in edges:
            if not isinstance(edge, dict):
                continue
            profile = dig(edge, "rendering_strategy", "view_model", "profile")
            if not isinstance(profile, dict):
                continue

            uid = profile.get("id")
            name = profile.get("name")
            if not uid or not name:
                continue
            uid = str(uid)

            # Facebook interleaves people into page results; keep them apart.
            entity = _category_for(profile, fallback=category)
            if entity is not category:
                continue
            if uid in seen:
                continue
            seen.add(uid)

            results.append(
                SearchResult(
                    id=uid,
                    name=str(name),
                    category=entity,
                    url=profile.get("url") or profile.get("profile_url"),
                    subtitle=_subtitle(profile),
                    avatar_url=dig(profile, "profile_picture", "uri"),
                    verified=bool(profile.get("is_verified")),
                )
            )

        return results


def _category_for(profile: dict[str, Any], *, fallback: SearchCategory) -> SearchCategory:
    """Infer the entity kind from ``__typename`` / page-specific fields.

    A ``User`` without any page metadata is a person — in *every* tab, so it is
    never reported as a Page. Facebook also returns real pages with
    ``__typename="User"`` in some surfaces; those always carry category fields.
    Unknown typenames keep the tab's ``fallback``.
    """
    typename = str(profile.get("__typename") or "")
    if typename in ("Page", "PageProfile"):
        return SearchCategory.PAGES
    if typename == "User":
        return SearchCategory.PAGES if _has_page_metadata(profile) else SearchCategory.PEOPLE
    return fallback


def _has_page_metadata(profile: dict[str, Any]) -> bool:
    """Whether a ``User``-typed node carries page-only fields."""
    for key in ("category_name", "category", "short_category_name"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _subtitle(profile: dict[str, Any]) -> str | None:
    for key in (
        "category_name",
        "category",
        "short_category_name",
        "location",
        "mutual_friends_text",
    ):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
