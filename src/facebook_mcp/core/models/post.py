"""Post domain models (L0 core layer)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Post(BaseModel):
    """A single Facebook post."""

    id: str = Field(..., description="Post id")
    text: str | None = Field(None, description="Post body text")
    created_at: datetime | None = Field(None, description="Publish time (UTC)")
    author_uid: str | None = Field(None, description="Author numeric id")
    url: str | None = Field(None, description="Permalink to the post")


class PostTimeResult(BaseModel):
    """Result of the ``get_user_latest_post_time`` tool."""

    ok: bool = Field(..., description="Whether the lookup succeeded")
    username: str = Field(..., description="Requested username")
    latest_post_time: datetime | None = Field(None, description="Publish time (UTC)")
    post_url: str | None = Field(None, description="Permalink of the latest post")
    post_text: str | None = Field(None, description="Snippet of the latest post")
    error: str | None = Field(None, description="Error message when ok=False")
