"""Messaging client (L3).

**Implementation note.** Modern Messenger web does not send messages through a
plain HTTP GraphQL mutation: the send is issued from Facebook's service worker
(observed via ``Network.requestWillBeSent`` on the page target, but not
interceptable there), and typing/sync traffic runs over a binary "Lightspeed"
WebSocket (``wss`` frames carry no readable message body). Re-implementing that
transport is out of scope, so this client drives the real Messenger UI through
the Chrome DevTools Protocol instead:

1. open the conversation — three fallbacks, cheapest first:
   ``/messages/t/<id>/`` → thread-list row → profile "*Message*" button
2. focus the composer, insert the text
3. click the *"Press enter to send"* button (Enter key as fallback)
4. confirm success by watching the composer clear
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..core.config import Settings
from ..core.exceptions import FacebookError, ParseError, UnsupportedError
from ..core.logger import get_logger
from ..core.models import SendResult
from ..session.manager import SessionManager
from ..transport.base import Transport
from ..transport.browsing import cdp_from
from ..transport.cdp import CdpError, CdpTransport
from .base import BaseClient
from .profile_client import ProfileClient

logger = get_logger(__name__)

MESSENGER_BASE = "https://www.facebook.com/messages/t/"
PROFILE_BASE = "https://www.facebook.com/profile.php?id="

_ID_PARAM = re.compile(r"[?&]id=(\d+)")

# Shared resolver for the *conversation* composer.
#
# The Messenger thread list also renders a `contenteditable` box labelled
# "Write to " (empty name) — that is the new-message search field, not a
# conversation. Requiring a non-empty name excludes it.
_RESOLVE_COMPOSER = """
  const editors = [...document.querySelectorAll('[contenteditable="true"]')];
  const composer = editors.find(
    el => /^Write to .+/i.test(el.getAttribute('aria-label') || '')
  );
  const sendButton = [...document.querySelectorAll('[aria-label]')].find(
    el => /press enter to send/i.test(el.getAttribute('aria-label') || '')
  );
"""


def _script(body: str) -> str:
    """Wrap ``body`` in an IIFE that has the composer resolver in scope."""
    return "(() => {" + _RESOLVE_COMPOSER + body + "})()"


class MessageClient(BaseClient):
    """Sends Messenger messages through the logged-in browser."""

    def __init__(
        self,
        transport: Transport,
        session: SessionManager,
        settings: Settings | None = None,
        profile_client: ProfileClient | None = None,
    ) -> None:
        super().__init__(transport, session, settings)
        self._profiles = profile_client

    # --- public API -----------------------------------------------------

    def send(self, recipient: str, text: str) -> SendResult:
        """Send ``text`` to ``recipient`` (username, vanity, uid or profile URL)."""
        text = text.strip()
        if not text:
            raise ValueError("message text must not be empty")

        uid = self.resolve_recipient(recipient)
        cdp = self._require_browser()

        thread_id = self._open_thread(cdp, uid)
        self._compose_and_send(cdp, text)

        return SendResult(ok=True, thread_id=thread_id, message_id=None)

    def resolve_recipient(self, recipient: str) -> str:
        """Turn a username / vanity / uid / profile URL into a numeric id."""
        recipient = recipient.strip()
        if not recipient:
            raise ValueError("recipient must not be empty")

        if recipient.isdigit():
            return recipient

        cleaned = recipient.split("?")[0].rstrip("/")
        if "facebook.com/" in cleaned:
            cleaned = cleaned.split("facebook.com/")[-1]
        cleaned = cleaned.split("/")[0].lstrip("@")

        if cleaned.isdigit():
            return cleaned

        # /profile.php?id=... style URLs carry the id in the query string.
        if cleaned == "profile.php":
            match = _ID_PARAM.search(recipient)
            if match:
                return match.group(1)

        if self._profiles is None:
            raise UnsupportedError(
                "cannot resolve usernames without a profile client; pass a numeric uid"
            )
        return self._profiles.resolve(cleaned).uid

    def send_via_graphql(self, recipient: str, text: str) -> SendResult:
        """Protocol-level send — not available yet (doc_id not captured)."""
        raise UnsupportedError(
            "Messenger sends go through Facebook's service worker / Lightspeed "
            "WebSocket, not a page-level GraphQL mutation. Use the browser path."
        )

    # --- browser plumbing -----------------------------------------------

    def _require_browser(self) -> CdpTransport:
        cdp = cdp_from(self._transport)
        if cdp is None:
            raise UnsupportedError(
                "send_message needs a browser session: set FB_TRANSPORT_MODE=auto "
                "or cdp and start Chrome with --remote-debugging-port=9222 "
                "(tools/launch_chrome_debug.sh)"
            )
        return cdp

    def _open_thread(self, cdp: CdpTransport, uid: str) -> str:
        """Open a conversation, falling back as far as the profile page."""
        logger.info("opening messenger thread %s", uid)
        try:
            cdp.navigate(f"{MESSENGER_BASE}{uid}/", timeout=45.0)
        except CdpError as exc:
            raise FacebookError(f"could not open Messenger thread for {uid}: {exc}") from exc

        if self._wait_for_composer(cdp, timeout=12):
            return self._thread_id(cdp, uid)

        # 1) The thread list may render without opening a conversation.
        logger.debug("composer missing after navigation; trying thread list row")
        if self._click_thread_row(cdp, uid) and self._wait_for_composer(cdp, timeout=15):
            return self._thread_id(cdp, uid)

        # 2) No existing thread (e.g. a page never messaged before): start one
        #    from the profile's "Message" button.
        logger.info("no conversation found; starting one from the profile page")
        if self._start_from_profile(cdp, uid) and self._wait_for_composer(cdp, timeout=20):
            return self._thread_id(cdp, uid)

        raise ParseError(
            f"Messenger composer did not appear for {uid}; the recipient may not "
            "accept messages, the thread id may be wrong, or the page layout changed"
        )

    def _start_from_profile(self, cdp: CdpTransport, uid: str) -> bool:
        """Open the profile and click its "Message" button."""
        try:
            cdp.navigate(f"{PROFILE_BASE}{uid}", timeout=45.0)
        except CdpError as exc:
            logger.info("could not open profile page for %s: %s", uid, exc)
            return False

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            position = self._message_button_position(cdp)
            if position:
                return self._click_at(cdp, position)
            time.sleep(0.5)

        logger.info("no Message button found on the profile of %s", uid)
        return False

    @staticmethod
    def _message_button_position(cdp: CdpTransport) -> dict[str, float] | None:
        """Viewport coordinates of the profile's Message button, if present."""
        raw = cdp.evaluate(
            """
(() => {
  const button = [...document.querySelectorAll('[role="button"],[aria-label]')]
    .find(el => {
      const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
      const text = (el.innerText || '').trim().toLowerCase();
      return label === 'message' || text === 'message';
    });
  if (!button) return null;
  button.scrollIntoView({block: 'center'});
  const rect = button.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
})()
"""
        )
        if isinstance(raw, dict) and "x" in raw and "y" in raw:
            return {"x": float(raw["x"]), "y": float(raw["y"])}
        return None

    @staticmethod
    def _click_at(cdp: CdpTransport, position: dict[str, float]) -> bool:
        """Dispatch a real mouse click (JS `.click()` misses React handlers)."""
        for event_type in ("mousePressed", "mouseReleased"):
            cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": position["x"],
                    "y": position["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )
        return True

    @staticmethod
    def _thread_id(cdp: CdpTransport, fallback: str) -> str:
        current = str(cdp.evaluate("location.href") or "")
        if "/messages/" in current:
            return current.rstrip("/").split("/")[-1] or fallback
        return fallback

    def _wait_for_composer(self, cdp: CdpTransport, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._has_composer(cdp):
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _click_thread_row(cdp: CdpTransport, uid: str) -> bool:
        clicked = cdp.evaluate(
            _script(
                f"""
  const target = {uid!r};
  const link = [...document.querySelectorAll('a[href*="/messages/"]')]
    .find(a => (a.getAttribute('href') || '').includes(target));
  if (!link) return false;
  link.click();
  return true;
"""
            )
        )
        return bool(clicked)

    @staticmethod
    def _has_composer(cdp: CdpTransport) -> bool:
        try:
            return bool(cdp.evaluate(_script("\n  return !!(composer || sendButton);\n")))
        except CdpError:
            return False

    # --- composing ------------------------------------------------------

    def _compose_and_send(self, cdp: CdpTransport, text: str) -> None:
        focused = cdp.evaluate(
            _script(
                """
  if (!composer) return 'missing';
  composer.focus();
  const range = document.createRange();
  range.selectNodeContents(composer);
  range.collapse(false);
  const selection = getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  return 'ok';
"""
            )
        )
        if focused != "ok":
            raise ParseError("could not focus the Messenger composer")

        cdp.send("Input.insertText", {"text": text})
        time.sleep(0.3)

        typed = self._composer_text(cdp)
        if text[:20] not in typed:
            raise ParseError(
                f"composer did not accept the text (got {typed!r}); aborting before send"
            )

        if not self._click_send(cdp):
            self._press_enter(cdp)

        if not self._wait_until_cleared(cdp):
            raise FacebookError(
                "message may not have been delivered: the composer still contains "
                "text after sending"
            )

    @staticmethod
    def _composer_text(cdp: CdpTransport) -> str:
        value: Any = cdp.evaluate(
            _script("\n  return composer ? (composer.innerText || '') : '';\n")
        )
        return str(value or "")

    @staticmethod
    def _click_send(cdp: CdpTransport) -> bool:
        clicked = cdp.evaluate(
            _script(
                """
  if (!sendButton) return false;
  sendButton.click();
  return true;
"""
            )
        )
        return bool(clicked)

    @staticmethod
    def _press_enter(cdp: CdpTransport) -> None:
        for event_type, key, text in (
            ("keyDown", "Enter", ""),
            ("char", "\r", "\r"),
            ("keyUp", "Enter", ""),
        ):
            cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": key,
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    "text": text,
                },
            )

    def _wait_until_cleared(self, cdp: CdpTransport, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._composer_text(cdp).strip():
                return True
            time.sleep(0.4)
        return False
