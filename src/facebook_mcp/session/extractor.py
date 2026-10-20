"""Session extraction from configuration and page HTML (L2)."""

from __future__ import annotations

import re
from pathlib import Path

from ..core.exceptions import AuthError
from ..core.logger import get_logger
from .state import SessionState

logger = get_logger(__name__)

# Token markers published by Facebook's ServerJS payload.
_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_DTSG_RE = re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"')
_DTSG_ALT_RE = re.compile(r'name="fb_dtsg"\s+value="([^"]+)"')
_USER_ID_RE = re.compile(r'"userID":"(\d+)"')


def from_cookie_string(raw: str, *, user_agent: str | None = None) -> SessionState:
    """Parse a ``name=value; name2=value2`` cookie header string."""
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    if not cookies:
        raise AuthError("FB_COOKIE does not contain any parseable cookie pairs")
    return SessionState(cookies=cookies, user_agent=user_agent)


def from_cookie_file(path: str | Path, *, user_agent: str | None = None) -> SessionState:
    """Parse a Netscape ``cookies.txt`` file (the format most extensions export)."""
    p = Path(path)
    if not p.is_file():
        raise AuthError(f"cookie file not found: {p}")

    cookies: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, _flag, _path, _secure, _expiry, name, value = fields[:7]
        if "facebook" not in domain:
            continue
        cookies[name] = value

    if not cookies:
        raise AuthError(f"no facebook.com cookies found in {p}")
    return SessionState(cookies=cookies, user_agent=user_agent)


def tokens_from_html(html: str) -> SessionState:
    """Extract ``lsd`` / ``fb_dtsg`` / cookies advertised by a page."""
    state = SessionState()

    if match := _LSD_RE.search(html):
        state.lsd = match.group(1)
    if (match := _DTSG_RE.search(html)) or (match := _DTSG_ALT_RE.search(html)):
        state.fb_dtsg = match.group(1)

    return state


def user_id_from_html(html: str) -> str | None:
    """Return the profile ``userID`` advertised by a profile page."""
    if match := _USER_ID_RE.search(html):
        return match.group(1)
    return None
