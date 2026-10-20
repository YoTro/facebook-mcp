"""Profile / timeline client (L3).

Reads the public profile page and extracts posts from the ServerJS payload
embedded in the HTML. No GraphQL ``doc_id`` is required for logged-out reads.

Verified structure (2026-10)::

    <script type="application/json" data-sjs>
      ... data.user.timeline_list_feed_units.edges[0].node
            .post_id / .creation_time / .permalink_url
            .comet_sections.content.story.message.text
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..core.extract import dig, find_first, iter_json_scripts, walk
from ..core.logger import get_logger
from ..core.models import Post, Profile
from ..core.models.profile import Timeline
from ..routes import rest
from .base import BaseClient

logger = get_logger(__name__)

TIMELINE_KEY = "timeline_list_feed_units"


class ProfileClient(BaseClient):
    """Fetches a profile and its timeline posts."""

    def fetch_timeline(self, username: str) -> Timeline:
        """Return the profile plus its first page of posts."""
        username = username.strip().lstrip("@")
        if not username:
            raise ValueError("username must not be empty")

        route = rest.PROFILE_PAGE
        resp = self._get(route, url=route.url(username=username), require_auth=False)
        html = resp.text
        self._session.learn_from_html(html)

        profile = self._parse_profile(html, username, final_url=resp.url)
        posts, complete = self._parse_posts(html)
        if not complete:
            logger.warning(
                "profile %r was served without a timeline payload (%d chars); "
                "the Accept header may have changed",
                username,
                len(html),
            )
        return Timeline(profile=profile, posts=posts, payload_complete=complete)

    def resolve(self, username: str) -> Profile:
        """Resolve a username to a :class:`Profile` without keeping posts."""
        return self.fetch_timeline(username).profile

    # --- parsing --------------------------------------------------------

    @staticmethod
    def _parse_profile(html: str, username: str, *, final_url: str) -> Profile:
        payloads = list(iter_json_scripts(html))

        uid = _find_vanity_anchored_uid(payloads) or _find_any_uid(payloads)
        if uid:
            return Profile(
                uid=uid,
                username=_find_vanity(payloads) or username,
                display_name=_find_display_name(payloads, uid),
                url=f"https://www.facebook.com/{username}",
            )

        # Last-resort regex fallback for a changed HTML shape.
        from ..session.extractor import user_id_from_html

        uid = user_id_from_html(html)
        if uid:
            logger.warning("profile parsed via regex fallback for %s", username)
            return Profile(uid=uid, username=username, url=final_url)
        raise ValueError(f"could not resolve profile '{username}' (page shape changed?)")

    @staticmethod
    def _parse_posts(html: str) -> tuple[list[Post], bool]:
        """Return ``(posts, payload_complete)``.

        ``payload_complete`` is False when no ``timeline_list_feed_units`` node
        existed at all, which means Facebook served the reduced (header-light)
        variant rather than a genuinely empty timeline.
        """
        for payload in iter_json_scripts(html):
            units = find_first(payload, TIMELINE_KEY)
            if not isinstance(units, dict):
                continue
            edges = units.get("edges") or []
            posts = [_node_to_post(edge.get("node")) for edge in edges]
            return [p for p in posts if p is not None], True
        return [], False


# --- payload helpers ------------------------------------------------------


def _nodes(payloads: list[Any]):
    for payload in payloads:
        for node in walk(payload):
            if isinstance(node, dict):
                yield node


def _find_vanity_anchored_uid(payloads: list[Any]) -> str | None:
    """Most reliable signal: a node carrying both ``userVanity`` and ``userID``."""
    for node in _nodes(payloads):
        if node.get("userVanity") and node.get("userID"):
            return str(node["userID"])
    return None


def _find_any_uid(payloads: list[Any]) -> str | None:
    for node in _nodes(payloads):
        uid = node.get("userID")
        if isinstance(uid, str) and uid.isdigit():
            return uid
    return None


def _find_vanity(payloads: list[Any]) -> str | None:
    for node in _nodes(payloads):
        if node.get("userVanity"):
            return str(node["userVanity"])
    return None


def _find_display_name(payloads: list[Any], uid: str) -> str | None:
    """Find the display name from an actor entry whose id matches ``uid``."""
    for node in _nodes(payloads):
        actors = node.get("actors")
        if not isinstance(actors, list):
            continue
        for actor in actors:
            if isinstance(actor, dict) and str(actor.get("id")) == uid and actor.get("name"):
                return str(actor["name"])
    return None


def _node_to_post(node: Any) -> Post | None:
    if not isinstance(node, dict) or "post_id" not in node:
        return None

    story = dig(node, "comet_sections", "content", "story") or {}
    if not isinstance(story, dict):
        story = {}
    message = story.get("message")
    text = message.get("text") if isinstance(message, dict) else None

    return Post(
        id=str(node.get("post_id") or node.get("id") or ""),
        text=text,
        created_at=_to_datetime(node.get("creation_time") or story.get("creation_time")),
        author_uid=_first_actor_id(node),
        url=node.get("permalink_url") or story.get("wwwURL"),
    )


def _first_actor_id(node: dict[str, Any]) -> str | None:
    actors = node.get("actors")
    if isinstance(actors, list) and actors and isinstance(actors[0], dict):
        uid = actors[0].get("id")
        return str(uid) if uid else None
    return None


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value), tz=UTC)
    return None
