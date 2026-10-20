"""Search use cases (L4)."""

from __future__ import annotations

from ..clients.search_client import SearchClient
from ..core.logger import get_logger
from ..core.models import SearchCategory, SearchResult

logger = get_logger(__name__)


class SearchService:
    """Keyword search across Facebook entity types."""

    def __init__(self, search_client: SearchClient) -> None:
        self._search = search_client

    def search_people(self, query: str, limit: int = 10) -> list[SearchResult]:
        return self._search.search(query, SearchCategory.PEOPLE, limit)

    def search_pages(self, query: str, limit: int = 10) -> list[SearchResult]:
        return self._search.search(query, SearchCategory.PAGES, limit)
