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
from ..core.exceptions import FacebookError, SendRejectedError
from ..core.logger import get_logger
from ..session.manager import SessionManager
from ..transport import lightspeed

logger = get_logger(__name__)

GATEWAY = "wss://gateway.facebook.com/ws/lightspeed"
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
    """What the gateway said about one send."""

    otid: str
    message_id: str | None
    rejected: bool
    raw: str


def gateway_url(uid: str, *, version: int = 5) -> str:
    """Build the gateway URL for ``uid`` (auth is cookie-based)."""
    return (
        f"{GATEWAY}?x-dgw-appid={lightspeed.APP_ID}&x-dgw-appversion=0"
        f"&x-dgw-authtype=1:0&x-dgw-version={version}&x-dgw-uuid={uid}"
        f"&x-dgw-tier=prod&x-dgw-loggingid={uuid.uuid4()}"
        f"&x-dgw-regionhint=FTW&x-dgw-deviceid={uuid.uuid4()}"
    )


class LightspeedSender:
    """Sends a text message over the Messenger sync WebSocket."""

    def __init__(
        self,
        settings: Settings,
        session: SessionManager,
        *,
        version_id: str = DEFAULT_VERSION_ID,
        connect_timeout: float = 15.0,
        reply_timeout: float = 15.0,
    ) -> None:
        self._settings = settings
        self._session = session
        self._version_id = version_id
        self._connect_timeout = connect_timeout
        self._reply_timeout = reply_timeout

    # --- public API -----------------------------------------------------

    def send(self, thread_key: str, text: str) -> Ack:
        """Deliver ``text`` to ``thread_key`` and return the gateway's verdict."""
        cookies = self._session.state.cookies
        uid = cookies.get("c_user")
        if not uid:
            raise LightspeedError("the session has no c_user cookie; cannot authenticate")

        ws = self._connect(uid, cookies)
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
        try:
            return websocket.create_connection(
                gateway_url(uid),
                timeout=self._connect_timeout,
                origin="https://www.facebook.com",
                header=[f"Cookie: {cookie_header}", f"User-Agent: {USER_AGENT}"],
            )
        except Exception as exc:
            raise LightspeedError(f"cannot open the Messenger gateway: {exc}") from exc

    def _warm_up_and_send(self, ws: Any, uid: str, thread_key: str, text: str) -> tuple[str, int]:
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
        ws.send_binary(lightspeed.encode_send(body, request_id))
        return otid, request_id

    def _await_verdict(self, ws: Any, otid: str, request_id: int) -> Ack:
        deadline = time.monotonic() + self._reply_timeout
        while time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except Exception as exc:
                raise LightspeedError(f"gateway closed before acknowledging: {exc}") from exc
            if not raw:
                continue
            payload = raw if isinstance(raw, bytes) else raw.encode()
            text = payload.decode("utf-8", "replace")

            if otid not in text:
                continue

            ack = parse_ack(text, otid)
            if ack.rejected:
                raise SendRejectedError(
                    'Messenger refused the message ("Couldn\'t send"); it did not '
                    "reach the recipient"
                )
            if ack.message_id is None:
                raise LightspeedError(
                    "the gateway referenced our otid without confirming a message id"
                )
            logger.info("lightspeed: delivered %s (mid=%s)", otid, ack.message_id)
            return ack

        raise LightspeedError(
            f"no acknowledgement within {self._reply_timeout:.0f}s (request {request_id})"
        )


def parse_ack(text: str, otid: str) -> Ack:
    """Extract the verdict for ``otid`` from a gateway frame.

    The gateway nests its steps inside a JSON *string*, so quotes arrive escaped
    (``\"replaceOptimsiticMessage\",\"<otid>\",\"mid.$...\"``). Matching the raw
    text misses that, and missing an acceptance is the dangerous direction: the
    message is delivered while the caller is told it was not.
    """
    normalized = text.replace('\\"', '"')
    rejected = REJECT_MARKER in normalized
    message_id = None
    if not rejected:
        marker = f'"{ACCEPT_MARKER}","{otid}"'
        index = normalized.find(marker)
        if index != -1:
            rest = normalized[index + len(marker) :]
            # … ,"mid.$cAA…" …
            if rest.startswith(","):
                candidate = rest[1:].lstrip()
                if candidate.startswith('"'):
                    message_id = candidate[1:].split('"', 1)[0] or None
    return Ack(otid=otid, message_id=message_id, rejected=rejected, raw=text[:400])
