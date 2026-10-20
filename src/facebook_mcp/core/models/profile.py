"""Profile domain models (L0 core layer)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .post import Post


class Profile(BaseModel):
    """A Facebook user or page profile."""

    uid: str = Field(..., description="Numeric Facebook id")
    username: str | None = Field(None, description="Vanity username, e.g. exampleuser")
    display_name: str | None = Field(None, description="Human-readable name")
    url: str | None = Field(None, description="Profile URL")
    avatar_url: str | None = Field(None, description="Profile picture URL")


class Timeline(BaseModel):
    """A profile together with a page of its posts (newest first)."""

    profile: Profile
    posts: list[Post] = Field(default_factory=list)
    payload_complete: bool = Field(
        True,
        description=(
            "False when the page carried no timeline section at all — Facebook "
            "sometimes serves a reduced variant (e.g. when the Accept header is "
            "missing), so an empty post list is not authoritative"
        ),
    )

    @property
    def latest_post(self) -> Post | None:
        """The newest post, if any."""
        timed = [p for p in self.posts if p.created_at is not None]
        if timed:
            return max(timed, key=lambda p: p.created_at)
        return self.posts[0] if self.posts else None
