"""Protocol-level Messenger sender (L3).

The UI path exists because the send *looked* unreplayable. It is replayable: the
page posts JSON tasks on ``wss://gateway.facebook.com/ws/lightspeed`` with
cookie auth (``x-dgw-authtype=1:0``), and the frame codec lives in
:mod:`facebook_mcp.transport.lightspeed`.

Sequence that the server accepts (verified live):

1. connect with the account's cookies,
2. post the thread-context fetches (menus, custom commands, contact questions),
3. post task 46 with the message,
4. the server answers ``replaceOptimsiticMessage`` plus a real ``mid.$…``;
   a refusal is ``markOptimisticMessageFailed / "Couldn't send."``.

No browser, no page, no DOM. The UI path stays as a fallback for anything the
protocol cannot reach (a thread that is not a 1:1 conversation key, an account
where the gateway is unreachable, …).
"""

from __future__ import annotations

import contextlib
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..core.config import Settings
from ..core.exceptions import FacebookError, SendRejectedError, SendUncertainError
from ..core.logger import get_logger
from ..session.manager import SessionManager
from ..transport import lightspeed
from ..transport.proxy import resolve_proxy
from ..transport.throttle import limiter_for

logger = get_logger(__name__)

GATEWAY = "wss://gateway.facebook.com/ws/lightspeed"
# Used only when the account has no UA configured; the transport's UA wins so a
# send and a search never present different browsers.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

# Version observed on every capture, across threads and sessions: it identifies
# the client build, not the queue, so it is a constant rather than session state.
DEFAULT_VERSION_ID = "39529440880004510"

ACCEPT_MARKER = "replaceOptimsiticMessage"
REJECT_MARKER = "markOptimisticMessageFailed"


class LightspeedError(FacebookError):
    """The protocol channel could not be used (network, handshake, no reply)."""

    code = "lightspeed_error"


@dataclass(frozen=True)
class Ack:
    """What the gateway said about one send.

    ``matched`` is False when the frame mentions our otid but carries no verdict
    step for it — the caller must keep reading instead of treating that as a
    failure.
    """

    otid: str
    message_id: str | None
    rejected: bool
    raw: str
    matched: bool = False


def gateway_url(uid: str, *, version: int = 5) -> str:
    """Build the gateway URL for ``uid`` (auth is cookie-based)."""
    return (
        f"{GATEWAY}?x-dgw-appid={lightspeed.APP_ID}&x-dgw-appversion=0"
        f"&x-dgw-authtype=1:0&x-dgw-version={version}&x-dgw-uuid={uid}"
        f"&x-dgw-tier=prod&x-dgw-loggingid={uuid.uuid4()}"
        f"&x-dgw-regionhint=FTW&x-dgw-deviceid={uuid.uuid4()}"
    )


class LightspeedSender:
    """Sends a text message over the Messenger sync WebSocket.

    ``warmup`` is off by default: a public page conversation accepts a bare task
    46 (measured). Some thread types — a page's own "get started" thread, for
    one — were only accepted after the thread-context fetches, so the option
    exists for those, but it is not a general requirement.
    """

    def __init__(
        self,
        settings: Settings,
        session: SessionManager,
        *,
        version_id: str = DEFAULT_VERSION_ID,
        warmup: bool = False,
        connect_timeout: float = 15.0,
        reply_timeout: float = 15.0,
    ) -> None:
        self._settings = settings
        self._session = session
        self._version_id = version_id
        self._warmup = warmup
        self._connect_timeout = connect_timeout
        self._reply_timeout = reply_timeout

    # --- public API -----------------------------------------------------

    def send(self, thread_key: str, text: str) -> Ack:
        """Deliver ``text`` to ``thread_key`` and return the gateway's verdict.

        Goes through :meth:`SessionManager.require_auth` like every other channel,
        so an empty account store still bootstraps from the browser and a session
        Facebook already rejected is refused here instead of on the wire.
        """
        state = self._session.require_auth()
        uid = state.user_id
        if not uid:
            raise LightspeedError("the session has no c_user cookie; cannot authenticate")

        # One logical operation, one slot in the account's shared budget.
        limiter_for(self._settings).wait()

        ws = self._connect(uid, state.cookies)
        try:
            otid, request_id = self._warm_up_and_send(ws, uid, thread_key, text)
            return self._await_verdict(ws, otid, request_id)
        finally:
            with contextlib.suppress(Exception):
                ws.close()

    # --- protocol steps -------------------------------------------------

    def _connect(self, uid: str, cookies: dict[str, str]) -> Any:
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LightspeedError("websocket-client is required") from exc

        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        user_agent = self._settings.user_agent or USER_AGENT
        options = self._proxy_options()
        try:
            return websocket.create_connection(
                gateway_url(uid),
                timeout=self._connect_timeout,
                origin="https://www.facebook.com",
                header=[f"Cookie: {cookie_header}", f"User-Agent: {user_agent}"],
                **options,
            )
        except Exception as exc:
            raise LightspeedError(f"cannot open the Messenger gateway: {exc}") from exc

    def _proxy_options(self) -> dict[str, Any]:
        """Forward the project's proxy configuration to the socket.

        The channel must leave through the same egress as curl_cffi: a search and
        a send from one account should not be seen from two different addresses.
        SOCKS needs PySocks, which is not a dependency; say so instead of silently
        connecting directly.
        """
        from urllib.parse import urlparse

        proxy = resolve_proxy(self._settings.proxy, self._settings.proxy_from_system)
        if not proxy:
            return {}

        parsed = urlparse(proxy)
        if not parsed.hostname or not parsed.port:
            raise LightspeedError(f"cannot parse the configured proxy {proxy!r}")

        scheme = (parsed.scheme or "http").lower()
        if scheme.startswith("socks"):
            try:
                import socks  # noqa: F401
            except ImportError as exc:
                raise LightspeedError(
                    f"proxy {proxy!r} needs SOCKS support in the WebSocket client; "
                    "install PySocks or point FB_PROXY at an http:// proxy"
                ) from exc
        return {
            "http_proxy_host": parsed.hostname,
            "http_proxy_port": parsed.port,
            "proxy_type": "http" if scheme.startswith("http") else "socks5",
        }

    def _warm_up_and_send(self, ws: Any, uid: str, thread_key: str, text: str) -> tuple[str, int]:
        if self._warmup:
            for index, tasks in enumerate(lightspeed.warmup_task_batches(thread_key, uid)):
                envelope = lightspeed.build_envelope(
                    tasks, request_id=10 + index, version_id=self._version_id
                )
                ws.send_binary(lightspeed.encode_send(envelope, 10 + index))
                time.sleep(0.03)

        otid = str(random.getrandbits(63))
        request_id = random.randint(1, 60000)
        body = lightspeed.build_send_body(
            thread_id=thread_key,
            text=text,
            otid=otid,
            request_id=request_id,
            version_id=self._version_id,
            epoch_id=random.getrandbits(63),
            data_trace_id="#" + uuid.uuid4().hex[:22],
        )
        frame = lightspeed.encode_send(body, request_id)
        try:
            ws.send_binary(frame)
        except Exception as exc:
            # The frame may have been partially written. Nothing before this point
            # could have delivered anything, so this is the one write whose failure
            # must not be reported as "not sent".
            raise SendUncertainError(
                "the message frame could not be confirmed as written "
                f"({exc}); check the conversation before resending"
            ) from exc
        return otid, request_id

    def _await_verdict(self, ws: Any, otid: str, request_id: int) -> Ack:
        deadline = time.monotonic() + self._reply_timeout
        while time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except Exception as exc:
                raise SendUncertainError(
                    f"the message was written but the gateway closed before "
                    f"acknowledging ({exc}); check the conversation before resending"
                ) from exc
            if not raw:
                continue
            payload = raw if isinstance(raw, bytes) else raw.encode()
            text = payload.decode("utf-8", "replace")

            if otid not in text:
                continue

            ack = parse_ack(text, otid)
            if not ack.matched:
                # Our otid shows up in an unrelated step (another message in the
                # same frame, a read receipt …). Keep waiting for our own verdict.
                continue

            if ack.rejected:
                # Deliberately does not guess *why*: "Couldn't send" is what the
                # server also answers when the recipient has messaging disabled,
                # when the thread type does not accept a protocol send, and when
                # context is missing. The caller can disambiguate with the UI.
                raise SendRejectedError(
                    'Messenger refused the message ("Couldn\'t send"); it did not '
                    "reach the recipient. The recipient may not accept messages "
                    "(a Page with messaging turned off), or this thread needs the "
                    "UI path"
                )
            if ack.message_id is None:
                # Accepted, but without a message id we cannot prove it — and the
                # answer is already on the wire.
                raise SendUncertainError(
                    "the gateway accepted the message but did not return a message "
                    "id; check the conversation before resending"
                )
            logger.info("lightspeed: delivered %s (mid=%s)", otid, ack.message_id)
            return ack

        # The frame was written; losing the acknowledgement says nothing about
        # delivery, so this must never look like "not sent".
        raise SendUncertainError(
            "the message was written but the gateway did not acknowledge within "
            f"{self._reply_timeout:.0f}s; check the conversation before resending"
        )


def parse_ack(text: str, otid: str) -> Ack:
    """Extract the verdict for ``otid`` from a gateway frame.

    The gateway nests its steps inside a JSON *string*, so quotes arrive escaped
    (``\"replaceOptimsiticMessage\",\"<otid>\",\"mid.$...\"``). Matching the raw
    text misses that, and missing an acceptance is the dangerous direction: the
    message is delivered while the caller is told it was not.
    """
    normalized = text.replace('\\"', '"')

    # Verdicts are per step and carry the otid they belong to. A frame may report
    # several messages (ours succeeding, another failing), so matching the marker
    # alone would blame our send for someone else's failure.
    reject_marker = f'"{REJECT_MARKER}","{otid}"'
    accept_marker = f'"{ACCEPT_MARKER}","{otid}"'

    if reject_marker in normalized:
        return Ack(otid=otid, message_id=None, rejected=True, raw=text[:400], matched=True)

    if accept_marker not in normalized:
        return Ack(otid=otid, message_id=None, rejected=False, raw=text[:400], matched=False)

    message_id = None
    rest = normalized[normalized.find(accept_marker) + len(accept_marker) :]
    if rest.startswith(","):
        candidate = rest[1:].lstrip()
        if candidate.startswith('"'):
            message_id = candidate[1:].split('"', 1)[0] or None
    return Ack(otid=otid, message_id=message_id, rejected=False, raw=text[:400], matched=True)
