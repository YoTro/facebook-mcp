"""Shared client base (L3)."""

from __future__ import annotations

from typing import Any

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
from ..transport.throttle import limiter_for

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
        self._limiter = limiter_for(settings) if settings is not None else None

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
        if not route.anonymous:
            # Profile pages are fetched without credentials on purpose: the
            # ServerJS payload only exists in the logged-out variant.
            merged.update(self._session.headers())
        if headers:
            merged.update(headers)

        # Protocol first, always: `require_auth()` above has already bootstrapped
        # cookies from the browser when it was allowed to, so by this point the
        # session either holds a usable jar (curl_cffi serves the request) or the
        # call has already failed. The old `from_config` hint sent every
        # browser-bootstrapped session back through CDP, which kept a browser
        # alive for reads the HTTP transport handles.
        #
        # The browser is still used deliberately elsewhere: cookie bootstrapping,
        # the Messenger UI path, and anything reached through `cdp_from()`.
        try:
            resp = self._transport.request(
                HttpRequest(
                    method=route.method,
                    url=url or (FACEBOOK_BASE + route.path),
                    headers=merged,
                    params=params,
                    data=data,
                    credentials="omit" if route.anonymous else "include",
                )
            )
        except AuthError:
            # The CDP transport raises AuthError from inside request() (it maps
            # the in-page status itself), so the status check below never runs.
            # Invalidate here too, or a rejected browser session keeps claiming
            # to be logged in and every later call repeats the same 403.
            if needs_auth:
                self._session.invalidate("the transport reported the credentials were rejected")
            raise
        try:
            self._raise_for_facebook_status(resp)
        except AuthError:
            # Only an authenticated request proves the credentials are dead;
            # a 403 on a public page must not throw the session away.
            if needs_auth:
                self._session.invalidate(f"HTTP {resp.status_code} on {resp.url}")
            raise
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
        payload = self._decode_graphql(resp, route, doc_id)
        if not isinstance(payload, dict):
            raise ParseError(
                f"{route.op}: unexpected GraphQL payload type {type(payload).__name__} "
                f"(doc_id {doc_id}); re-capture with tools/sniff.py --save-templates"
            )
        self._raise_for_legacy_error(payload, route, doc_id)
        return payload

    @staticmethod
    def _decode_graphql(resp: HttpResponse, route: Route, doc_id: str) -> Any:
        """Decode a GraphQL body, turning decode failures into ``ParseError``.

        A rotated or mistyped ``doc_id`` makes Facebook answer HTTP 200 with a
        legacy error envelope (``for (;;);{"error":1357031,...}``) instead of
        Relay JSON, so an unhandled ``JSONDecodeError`` would hide the real
        cause. ``HttpResponse.json`` strips the guard prefix; anything still
        undecodable is a genuine shape change.
        """
        try:
            return resp.json()
        except ValueError:
            snippet = resp.text[:120].replace("\n", " ")
            raise ParseError(
                f"{route.op}: response was not JSON ({snippet!r}); the doc_id "
                f"{doc_id} may have rotated — re-capture with "
                "tools/sniff.py --save-templates"
            ) from None

    def _raise_for_legacy_error(
        self, payload: dict[str, object], route: Route, doc_id: str
    ) -> None:
        """Translate Facebook's legacy ``{"error": <code>, ...}`` envelope.

        Relay failures use ``"errors": [...]`` (handled by
        :meth:`_raise_for_graphql_errors`). The legacy shape appears when the
        query id is unknown to the current build, which is the common outcome of
        a ``doc_id`` rotation.
        """
        if "errors" in payload or "error" not in payload:
            return

        code = payload.get("error")
        summary = str(payload.get("errorSummary") or "")
        description = str(payload.get("errorDescription") or "")
        detail = ("; ".join(part for part in (summary, description) if part) or "no details")[:160]

        # 1357031 == "This content is no longer available": the query id the
        # server was given does not exist in the build it is serving.
        rotated = code == 1357031 or "no longer available" in detail.lower()
        hint = (
            f"; the doc_id {doc_id} was rejected — it has probably rotated, "
            "re-capture with tools/sniff.py --save-templates"
            if rotated
            else f"; doc_id {doc_id}"
        )
        raise ParseError(f"{route.op}: Facebook error {code} ({detail}){hint}")

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

        # A doc_id the current build does not know also arrives as a Relay
        # error: 'The GraphQL document with ID 1 was not found.'
        if "graphql document with id" in lowered or (
            "document" in lowered and "not found" in lowered
        ):
            raise ParseError(
                f"{op}: Facebook rejected the query id ({joined}); it has probably "
                "rotated — re-capture with tools/sniff.py --save-templates"
            )

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
            if self._session.is_authenticated:
                self._session.invalidate(f"{op}: {joined}")
            raise AuthError(f"{op}: {joined}")
        raise ParseError(f"{op} failed: {joined}")


def _json_dumps(value: object) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
