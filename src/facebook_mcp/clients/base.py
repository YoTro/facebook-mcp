"""Shared client base (L3)."""

from __future__ import annotations

from ..core.config import Settings
from ..core.exceptions import (
    AuthError,
    HttpStatusError,
    NotFoundError,
    ParseError,
    RateLimitError,
)
from ..core.logger import get_logger
from ..routes.base import FACEBOOK_BASE, GRAPHQL_PATH, Route
from ..session.manager import SessionManager
from ..transport.base import HttpRequest, HttpResponse, Transport

logger = get_logger(__name__)

_DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class BaseClient:
    """Common request plumbing for every domain client.

    Collaborators are injected (transport + session) so clients stay testable
    and unaware of how the request physically travels.
    """

    def __init__(
        self,
        transport: Transport,
        session: SessionManager,
        settings: Settings | None = None,
    ) -> None:
        self._transport = transport
        self._session = session
        self._settings = settings

    # --- low-level ------------------------------------------------------

    def _request(
        self,
        route: Route,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | str | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
        require_auth: bool | None = None,
    ) -> HttpResponse:
        needs_auth = route.requires_auth if require_auth is None else require_auth
        if needs_auth:
            self._session.require_auth()

        merged = dict(_DEFAULT_HEADERS)
        merged.update(self._session.headers())
        if headers:
            merged.update(headers)

        # Authenticated operations run inside the browser when a hybrid/CDP
        # transport is active; plain HTTP transports simply ignore the hint.
        # Protocol first: configured cookies mean curl_cffi can do the job
        # itself; otherwise the browser supplies the login state.
        hint: str | None = None
        if needs_auth and not self._session.from_config:
            hint = "cdp"

        resp = self._transport.request(
            HttpRequest(
                method=route.method,
                url=url or (FACEBOOK_BASE + route.path),
                headers=merged,
                params=params,
                data=data,
                transport_hint=hint,
            )
        )
        self._raise_for_facebook_status(resp)
        return resp

    def _get(
        self,
        route: Route,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
        require_auth: bool | None = None,
    ) -> HttpResponse:
        return self._request(
            route, params=params, headers=headers, url=url, require_auth=require_auth
        )

    def _doc_id(self, route: Route) -> str | None:
        """Resolve the ``doc_id`` for ``route``.

        Precedence: the account store (``account.json`` -> ``doc_ids``) wins
        over the registry in ``routes/graphql.py``. The registry entry is only a
        bootstrap default: Facebook rotates ``doc_id`` with every frontend build
        and may serve different builds to different accounts, so the live value
        lives in the credential store, where refreshing it needs no commit.
        """
        override = None
        if self._settings is not None:
            override = self._settings.route_doc_ids.get(route.op)
        if override:
            if override != route.doc_id:
                logger.debug("doc_id for %s from account store: %s", route.op, override)
            return override
        return route.doc_id

    def _graphql(
        self,
        route: Route,
        variables: dict[str, object],
        *,
        friendly_name: str | None = None,
    ) -> dict[str, object]:
        doc_id = self._doc_id(route)
        if not doc_id:
            raise AuthError(
                f"GraphQL route '{route.op}' has no doc_id; capture it with tools/sniff.py"
            )
        state = self._session.require_auth()
        self._ensure_dtsg()
        state = self._session.state

        dtsg = state.fb_dtsg or ""
        form = {
            "av": state.user_id or "0",
            "__user": state.user_id or "0",
            "__a": "1",
            "fb_dtsg": dtsg,
            "lsd": state.lsd or "",
            # jazoest is derived from fb_dtsg: "2" followed by the sum of the
            # character codes. Verified against live traffic.
            "jazoest": "2" + str(sum(ord(c) for c in dtsg)),
            "doc_id": doc_id,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name or route.friendly_name or "",
            "variables": _json_dumps(variables),
            "server_timestamps": "true",
        }
        resp = self._request(
            Route(op=route.op, method="POST", path=GRAPHQL_PATH, requires_auth=True),
            data=form,
            url=FACEBOOK_BASE + GRAPHQL_PATH,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-FB-Friendly-Name": friendly_name or route.friendly_name or "",
            },
        )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise AuthError(f"unexpected GraphQL payload for {route.op}")
        return payload

    def _ensure_dtsg(self) -> None:
        """Fetch the home page once to learn ``fb_dtsg`` / ``lsd`` when missing."""
        state = self._session.state
        if state.fb_dtsg and state.lsd:
            return
        if not state.is_authenticated:
            return

        logger.debug("fetching home page to obtain fb_dtsg/lsd")
        resp = self._request(
            Route(op="session.home", method="GET", path="/"),
            url=FACEBOOK_BASE + "/",
            require_auth=False,
        )
        self._session.learn_from_html(resp.text)

    # --- error translation ---------------------------------------------

    @staticmethod
    def _raise_for_facebook_status(resp: HttpResponse) -> None:
        if resp.status_code == 429:
            raise RateLimitError(f"rate limited on {resp.url}")
        if resp.status_code == 404:
            raise NotFoundError(f"not found: {resp.url}")
        if resp.status_code in (401, 403):
            raise AuthError(f"access denied (HTTP {resp.status_code}): {resp.url}")
        if resp.status_code >= 400:
            raise HttpStatusError(
                f"HTTP {resp.status_code} for {resp.url}", status_code=resp.status_code
            )

    def _raise_for_graphql_errors(self, payload: dict[str, object], op: str) -> None:
        """Translate Relay ``errors`` into the project's exception types."""
        errors = payload.get("errors")
        if not isinstance(errors, list) or not errors:
            return

        messages = [str(e.get("message") if isinstance(e, dict) else e) for e in errors]
        codes = [e.get("code") for e in errors if isinstance(e, dict)]
        joined = "; ".join(messages)[:400]
        lowered = joined.lower()

        if "missing_required_variable" in lowered:
            raise ParseError(
                f"{op}: captured GraphQL variables/template are stale ({joined}); "
                "re-capture with tools/sniff.py --save-templates"
            )

        # Facebook answers unauthenticated Relay calls with a generic
        # "Rate limit exceeded" (code 1675004) instead of an auth error, which
        # is misleading. Distinguish it from a real rate limit.
        anonymous_rate_limit = 1675004 in codes or (
            "rate limit" in lowered and not self._session.is_authenticated
        )
        if anonymous_rate_limit and not self._session.is_authenticated:
            raise AuthError(
                f"{op}: requires a logged-in session (Facebook returns a generic "
                f"'{joined}' for anonymous Relay calls). Configure cookies for this "
                "account, e.g. tools/export_cookies.py --account <name>"
            )
        if "rate limit" in lowered:
            raise RateLimitError(f"{op}: {joined}")

        if any(
            marker in lowered
            for marker in (
                "log in",
                "login",
                "logged out",
                "permission",
                "unauthorized",
                "checkpoint",
            )
        ):
            raise AuthError(f"{op}: {joined}")
        raise ParseError(f"{op} failed: {joined}")


def _json_dumps(value: object) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
