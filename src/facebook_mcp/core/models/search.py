"""Search domain models (L0 core layer)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SearchCategory(StrEnum):
    """Kind of entity returned by a search."""

    PEOPLE = "people"
    PAGES = "pages"


class SearchResult(BaseModel):
    """A single search hit."""

    id: str = Field(..., description="Numeric id (uid or page id)")
    name: str = Field(..., description="Display name")
    category: SearchCategory = Field(..., description="people or pages")
    url: str | None = Field(None, description="Profile/page URL")
    subtitle: str | None = Field(None, description="Secondary line (location / page category)")
    avatar_url: str | None = Field(None, description="Avatar image URL")
    verified: bool = Field(False, description="Whether the entity is verified")
