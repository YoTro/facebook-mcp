"""Post use cases (L4)."""

from __future__ import annotations

from ..clients.profile_client import ProfileClient
from ..core.exceptions import FacebookError, NotFoundError
from ..core.logger import get_logger
from ..core.models import PostTimeResult

logger = get_logger(__name__)


class PostService:
    """Answer questions about a user's posts."""

    def __init__(self, profile_client: ProfileClient) -> None:
        self._profiles = profile_client

    def get_latest_post_time(self, username: str) -> PostTimeResult:
        """Return the publish time of the newest post for ``username``."""
        try:
            timeline = self._profiles.fetch_timeline(username)
        except FacebookError as exc:
            return PostTimeResult(ok=False, username=username, error=str(exc))
        except ValueError as exc:
            return PostTimeResult(ok=False, username=username, error=str(exc))

        latest = timeline.latest_post
        if latest is None:
            hint = ""
            if not timeline.payload_complete:
                hint = (
                    " — the page was served without a timeline payload; "
                    "if this profile does have posts, retry (Facebook sometimes "
                    "returns a reduced page)"
                )
            return PostTimeResult(
                ok=False,
                username=username,
                error=f"no posts found for '{username}'{hint}",
            )

        return PostTimeResult(
            ok=True,
            username=timeline.profile.username or username,
            latest_post_time=latest.created_at,
            post_url=latest.url,
            post_text=(latest.text or "")[:280] or None,
        )

    def get_recent_posts(self, username: str, limit: int = 10) -> list:
        """Return up to ``limit`` recent posts, newest first."""
        limit = max(1, min(int(limit), 50))
        timeline = self._profiles.fetch_timeline(username)
        if not timeline.posts:
            raise NotFoundError(f"no posts found for '{username}'")
        timed = [p for p in timeline.posts if p.created_at is not None]
        ordered = sorted(timed, key=lambda p: p.created_at, reverse=True)  # type: ignore[arg-type,return-value]
        return ordered[:limit] if timed else timeline.posts[:limit]
