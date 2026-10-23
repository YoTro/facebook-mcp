"""Session lifecycle management (L2)."""

from __future__ import annotations

import time
from collections.abc import Callable

from ..core.config import Settings
from ..core.exceptions import AuthError
from ..core.logger import get_logger
from . import extractor
from .state import SessionState

logger = get_logger(__name__)

CookieProvider = Callable[[], dict[str, str]]


class SessionManager:
    """Owns the active :class:`SessionState` and keeps its tokens fresh.

    ``cookie_provider`` is an optional callable used to bootstrap cookies from
    a live browser (Chrome DevTools) when no cookie file/string is configured.
    Keeping it a plain callable keeps this layer independent of the CDP
    implementation.
    """

    def __init__(self, settings: Settings, cookie_provider: CookieProvider | None = None) -> None:
        self._settings = settings
        self._cookie_provider = cookie_provider
        # True when cookies came from configuration / the account store rather
        # than the browser. Drives the "protocol first" transport choice.
        self.from_config = bool(
            settings.cookie
            or settings.cookie_file
            or settings.account_paths().cookies_file.is_file()
        )
        # Where the cookies in use actually came from; invalidate() must only
        # discard a jar this process bootstrapped, never one the user handed us.
        self._source: str = "none"
        self._state = self._load(settings)
        # Set when Facebook rejected the credentials; see invalidate().
        self._rejected: str | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_authenticated(self) -> bool:
        return self._state.is_authenticated

    def headers(self) -> dict[str, str]:
        """Headers to attach to outgoing requests."""
        return self._state.as_headers()

    def require_auth(self) -> SessionState:
        """Return the state, raising :class:`AuthError` when not logged in."""
        if self.is_authenticated:
            if self._rejected:
                # An exported jar cannot be refreshed in-process: fail fast
                # instead of replaying a cookie Facebook already refused.
                raise AuthError(
                    f"the session was rejected by Facebook ({self._rejected}) and "
                    "the cookies come from a file/string, which cannot be refreshed "
                    "in process; re-export them (tools/export_cookies.py --account "
                    f"{self._settings.account}) and restart the server"
                )
            return self._state
        if self._bootstrap_from_browser():
            return self._state
        raise AuthError(
            "this operation requires a logged-in session; set FB_COOKIE / "
            "FB_COOKIE_FILE, or start Chrome with --remote-debugging-port=9222 "
            "while logged in to Facebook"
        )

    def invalidate(self, reason: str) -> None:
        """React to Facebook rejecting the current credentials.

        Always drops the CSRF tokens, which rotate and are cheap to re-learn.
        A browser-backed session also drops the cookies so the next
        :meth:`require_auth` re-reads the live jar — that is the only channel
        that can actually refresh. An exported jar is kept (the user may still
        read it) but marked, so the next authenticated call fails with advice
        instead of looping on a dead cookie.
        """
        self._rejected = reason or "rejected by Facebook"
        self._state.fb_dtsg = None
        self._state.lsd = None

        if self._source == "bootstrap":
            # Everything in the jar arrived from the browser, and we wrote it to
            # the store ourselves: drop both so the next call bootstraps again.
            self._state.cookies.clear()
            self._discard_persisted()
        elif not self.from_config:
            self._state.cookies.clear()
        # A jar the user supplied (FB_COOKIE / FB_COOKIE_FILE) is left untouched:
        # clearing it in memory is enough, and its file is not ours to delete.
        logger.info("session invalidated (%s)", self._rejected)

    def _discard_persisted(self) -> None:
        paths = self._settings.account_paths()
        try:
            if paths.cookies_file.is_file():
                paths.cookies_file.unlink()
            paths.merge_meta(source="invalidated")
            logger.info("discarded persisted bootstrapped cookies")
        except OSError as exc:  # pragma: no cover - disk/permission problems
            logger.warning("could not discard persisted cookies: %s", exc)

    @property
    def rejected(self) -> str | None:
        """Why Facebook last rejected the session, if it did."""
        return self._rejected

    def bootstrap_from_browser(self) -> bool:
        """Adopt cookies from the attached browser. Returns success."""
        return self._bootstrap_from_browser()

    def learn_from_html(self, html: str) -> None:
        """Pick up rotated tokens (lsd / fb_dtsg) from a fetched page."""
        discovered = extractor.tokens_from_html(html)
        before = (self._state.fb_dtsg, self._state.lsd)
        self._state.merge_tokens(discovered)
        after = (self._state.fb_dtsg, self._state.lsd)
        if after != before and after[1]:
            logger.debug("session tokens refreshed (lsd=%s)", after[1][:6])

    def set_cookie(self, name: str, value: str) -> None:
        self._state.cookies[name] = value

    def _load(self, settings: Settings) -> SessionState:
        """Load cookies, preferring the isolated account directory.

        Priority:``FB_COOKIE_FILE`` → ``<home>/accounts/<account>/cookies.txt`` → ``FB_COOKIE``.
        """
        if settings.cookie_file:
            self._source = "cookie-file"
            state = extractor.from_cookie_file(settings.cookie_file, user_agent=settings.user_agent)
            logger.info(
                "account %r: loaded %d cookies from FB_COOKIE_FILE (authenticated=%s)",
                settings.account,
                len(state.cookies),
                state.is_authenticated,
            )
            return state

        account_paths = settings.account_paths()
        if account_paths.cookies_file.is_file():
            # A jar this tool wrote after bootstrapping stays *refreshable*: if
            # Facebook rejects it, invalidate() must be allowed to discard it and
            # bootstrap again, exactly as it would in the process that wrote it.
            meta = account_paths.load_meta()
            self._source = "bootstrap" if meta.get("source") == "cdp-bootstrap" else "account-store"
            warning = account_paths.permission_warning()
            if warning:
                logger.warning(warning)
            state = extractor.from_cookie_file(
                account_paths.cookies_file, user_agent=settings.user_agent
            )
            logger.info(
                "account %r: loaded %d cookies from %s (authenticated=%s)",
                settings.account,
                len(state.cookies),
                account_paths.cookies_file,
                state.is_authenticated,
            )
            return state

        if settings.cookie:
            self._source = "cookie"
            state = extractor.from_cookie_string(settings.cookie, user_agent=settings.user_agent)
            logger.warning(
                "account %r: using FB_COOKIE from the environment; prefer %s for isolation",
                settings.account,
                account_paths.cookies_file,
            )
            return state

        logger.info(
            "account %r: no cookies found (looked in %s); running anonymously (public pages only)",
            settings.account,
            account_paths.cookies_file,
        )
        return SessionState(user_agent=settings.user_agent)

    def _bootstrap_from_browser(self) -> bool:
        """Pull cookies from the attached Chrome, if a provider is wired up."""
        if self._cookie_provider is None:
            return False
        try:
            cookies = self._cookie_provider()
        except Exception as exc:  # noqa: BLE001 - the browser may not be running
            logger.info("could not bootstrap cookies from browser: %s", exc)
            return False

        if not cookies:
            return False

        # Replace, never merge: `update()` keeps cookies the new snapshot does not
        # have (an `i_user` from the previous Page identity survives an account
        # switch and then lies to every actor probe).
        previous_id = self._state.user_id
        self._state.cookies = dict(cookies)
        # CSRF tokens belong to the login we just replaced; they are cheap to
        # re-learn, and keeping them mixes two identities' credentials.
        self._state.fb_dtsg = None
        self._state.lsd = None
        if previous_id and self._state.user_id != previous_id:
            logger.warning(
                "browser session switched identity from c_user=%s to c_user=%s",
                previous_id,
                self._state.user_id,
            )
        if not self._state.is_authenticated:
            logger.warning(
                "got %d browser cookies but no c_user/xs — Chrome is not logged in to Facebook",
                len(cookies),
            )
            return False
        self._rejected = None
        self._source = "bootstrap"
        logger.info("bootstrapped %d cookies from browser", len(cookies))
        self._persist_bootstrapped(cookies)
        return True

    @property
    def source(self) -> str:
        """Where the active cookies came from: bootstrap / account-store / cookie*."""
        return self._source

    def _persist_bootstrapped(self, cookies: dict[str, str]) -> None:
        """Store browser cookies in the account store for browser-free runs.

        The exported jar then outranks the browser on the next start, so a single
        login (or a single attached Chrome) is enough. Skipped when the user
        manages credentials themselves, and the file is marked so
        :meth:`invalidate` knows it may delete it.
        """
        settings = self._settings
        if not settings.persist_browser_cookies:
            return
        if settings.cookie or settings.cookie_file:
            return  # the user owns this jar; do not shadow it

        paths = settings.account_paths()
        expiry = int(time.time()) + 30 * 86400
        lines = [
            "# Netscape HTTP Cookie File",
            "# Written by facebook-mcp after bootstrapping from Chrome.",
        ]
        for name, value in sorted(cookies.items()):
            lines.append(
                "\t".join([".facebook.com", "TRUE", "/", "TRUE", str(expiry), name, value])
            )
        try:
            paths.ensure()
            paths.cookies_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.cookies_file.chmod(0o600)
            paths.merge_meta(
                source="cdp-bootstrap", c_user=cookies.get("c_user"), updated_at=int(time.time())
            )
            logger.info("persisted bootstrapped cookies to %s", paths.cookies_file)
        except OSError as exc:  # pragma: no cover - disk/permission problems
            logger.warning("could not persist bootstrapped cookies: %s", exc)
