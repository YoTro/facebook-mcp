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
2. verify the composer addresses the intended recipient (thread id or name),
   re-checking immediately before the click
3. insert the text, refusing when the box already holds a draft
4. click the *"Press enter to send"* button (Enter key as fallback)
5. classify the outcome: cleared composer = ``submitted``; a new "Couldn't send"
   label on our bubble = ``rejected``; anything unobservable after the click =
   ``unknown``. A cleared composer alone is *not* proof of delivery.

The third fallback is unavailable to a Page: Facebook does not render the
profile "Message" button while the session acts as a Page, because a Page
cannot start a conversation with someone who never messaged it. Such a session
is detected from the ``i_user`` cookie and fails fast with an explanation
instead of polling for a button that will never appear.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..core.config import Settings
from ..core.exceptions import (
    FacebookError,
    ParseError,
    SendRejectedError,
    SendUncertainError,
    TransportError,
    UnsupportedError,
)
from ..core.logger import get_logger
from ..core.models import SendResult
from ..session.manager import SessionManager
from ..transport.base import Transport
from ..transport.browsing import BrowserTransport, cdp_from
from .base import BaseClient
from .profile_client import ProfileClient

logger = get_logger(__name__)

MESSENGER_BASE = "https://www.facebook.com/messages/t/"
PROFILE_BASE = "https://www.facebook.com/profile.php?id="

_ID_PARAM = re.compile(r"[?&]id=(\d+)")
# Either /messages/t/<id>/ or the e2ee variant /messages/e2ee/t/<id>/.
_THREAD_IN_URL = re.compile(r"/messages/(?:e2ee/)?t/([0-9]+)")

_PAGE_ACTOR_REFUSAL = (
    "cannot start a conversation with {uid}: this session is acting as a Page "
    '(i_user cookie), and Facebook hides the profile "Message" button for Page '
    "actors. Reply to an existing thread instead, or use a personal account "
    "(FB_ACCOUNT=<name>)."
)

# Shared resolver for the *conversation* composer.
#
# Two traps here:
#   * the thread list renders a `contenteditable` box labelled "Write to "
#     (empty name) — the new-message search field, not a conversation;
#   * docked chat popups each render their own "Write to <name>" composer, so a
#     plain querySelector can pick the wrong person's box.
# Only a labelled composer inside the main pane counts, and its label is exposed
# as ``composerTarget`` so the caller can verify the recipient before sending.
_RESOLVE_COMPOSER = """
  const isDocked = (el) => !!el.closest(
    '[role="complementary"], [aria-label="Chats"], [aria-label="Chat tabs"], [data-pagelet="ChatTab"]'
  );
  const candidates = [...document.querySelectorAll('[contenteditable="true"]')]
    .map(el => ({el, label: (el.getAttribute('aria-label') || '')}))
    .filter(c => /^Write to .+/i.test(c.label))
    .filter(c => !isDocked(c.el));
  const chosen = candidates[0] || null;
  const composer = chosen ? chosen.el : null;
  const composerTarget = chosen
    ? chosen.label.replace(/^Write to /i, '').trim()
    : null;
  const pane = composer
    ? (composer.closest('[role="main"]') || composer.parentElement)
    : null;
  const sendButton = pane
    ? [...pane.querySelectorAll('[aria-label]')].find(
        el => /press enter to send/i.test(el.getAttribute('aria-label') || '')
      )
    : null;
"""


# Messenger renders this marker inside a bubble it could not send. It is UI
# text, so it is localised — the list is best-effort and easy to extend. The
# a11y label of the same bubble still says "Message sent", so the text is the
# only usable signal.
_FAILED_BUBBLE_MARKERS = ("Couldn't send", "无法发送", "無法傳送", "No se pudo enviar")


def _failed_bubbles(cdp: BrowserTransport, needle: str | None = None) -> int:
    """Status labels in the open conversation that say "not sent".

    Only *leaf* nodes whose whole trimmed text is exactly a marker count. An
    earlier version searched article bodies for the marker, so a perfectly normal
    message such as "fixed the Couldn't send issue" incremented the counter and
    was reported as rejected — a false alarm that invites a duplicate resend.

    ``needle`` (a prefix of the text we just sent) restricts the count to the
    bubble we produced, so a concurrent failure in another message cannot taint
    this send.
    """
    markers = ", ".join(json.dumps(marker) for marker in _FAILED_BUBBLE_MARKERS)
    prefix = json.dumps(needle) if needle else "null"
    count: Any = cdp.evaluate(
        _script(
            f"""
  const markers = [{markers}];
  const needle = {prefix};
  const main = document.querySelector('[role="main"]');
  if (!main) return 0;
  let failed = 0;
  for (const node of main.querySelectorAll('*')) {{
    if (node.childElementCount !== 0) continue;          // status text is a leaf
    const text = (node.textContent || '').trim();
    if (!markers.includes(text)) continue;               // exact, not "contains"
    if (needle) {{
      const article = node.closest('[role="article"]');
      if (!article || !(article.innerText || '').includes(needle)) continue;
    }}
    failed += 1;
  }}
  return failed;
"""
        )
    )
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


# Facebook shortens long names in the composer label ("Write to Alice Sm…").
_TRUNCATION_SUFFIXES = ("\u2026", "...")


def _same_name(actual: str, expected: str) -> bool:
    """Whether two Messenger display names plausibly denote the same account.

    A plain prefix is *not* enough: "Example User" is a prefix of "Example User
    Two", and treating them as equal would send to the wrong person. Only an
    explicit truncation marker makes the shorter name a candidate, and the longer
    one must still start with the visible prefix.
    """
    a = " ".join(actual.split()).casefold()
    b = " ".join(expected.split()).casefold()
    if len(a) < 3 or len(b) < 3:
        return False
    if a == b:
        return True

    for short, long in ((a, b), (b, a)):
        for suffix in _TRUNCATION_SUFFIXES:
            if not short.endswith(suffix):
                continue
            stem = short[: -len(suffix)].rstrip()
            if len(stem) >= 3 and long.startswith(stem):
                return True
    return False


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

        thread_id, composer_target = self._open_thread(cdp, uid)
        try:
            self._compose_and_send(cdp, text, expected_target=composer_target)
        except SendRejectedError as exc:
            # The caller needs to know which thread was affected.
            exc.thread_id = thread_id
            raise

        return SendResult(
            ok=True,
            delivery="submitted",
            thread_id=thread_id,
            message_id=None,
        )

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
        """Protocol-level send — kept as an explicit dead end.

        Messenger does not expose a page-level GraphQL mutation for sending
        (verified: the request is issued by the service worker, and the
        Lightspeed WebSocket frames carry no readable body). See
        ARCHITECTURE §8.3 for the evidence.
        """
        raise UnsupportedError(
            "Messenger sends go through Facebook's service worker / Lightspeed "
            "WebSocket, not a page-level GraphQL mutation. Use the browser path."
        )

    # --- browser plumbing -----------------------------------------------

    def _require_browser(self) -> BrowserTransport:
        cdp = cdp_from(self._transport)
        if cdp is None:
            raise UnsupportedError(
                "send_message needs a browser session: set FB_TRANSPORT_MODE=auto "
                "or cdp and start Chrome with --remote-debugging-port=9222 "
                "(tools/launch_chrome_debug.sh)"
            )
        return cdp

    def _pace(self) -> None:
        """Respect ``min_request_interval`` for UI-driven operations too.

        Navigating Messenger and clicking send issue the same kind of traffic as
        a GraphQL call, so they draw from the account's shared budget instead of
        bypassing the limiter the HTTP transport uses.
        """
        if self._limiter is not None:
            self._limiter.wait()

    def _open_thread(self, cdp: BrowserTransport, uid: str) -> tuple[str, str | None]:
        """Open the conversation for ``uid`` and verify it is the right one.

        Returns ``(thread_id, composer_target)``; ``composer_target`` is the name
        from the composer's "Write to <name>" label, used to spot a re-render
        that swapped the conversation while composing.
        """
        logger.info("opening messenger thread %s", uid)
        self._pace()
        try:
            cdp.navigate(f"{MESSENGER_BASE}{uid}/", timeout=45.0)
        except TransportError as exc:
            raise FacebookError(f"could not open Messenger thread for {uid}: {exc}") from exc

        if self._wait_for_composer(cdp, timeout=12):
            return self._confirm_thread(cdp, uid, how="direct thread URL")

        # 1) The thread list may render without opening a conversation.
        logger.debug("composer missing after navigation; trying thread list row")
        if self._click_thread_row(cdp, uid) and self._wait_for_composer(cdp, timeout=15):
            return self._confirm_thread(cdp, uid, how="thread-list row")

        # 2) No existing thread (e.g. a page never messaged before): start one
        #    from the profile's "Message" button. A Page cannot do this —
        #    Facebook does not render the button for Page actors — so refuse
        #    before polling 15s + 20s for something that will never appear.
        if self._acting_as_page(cdp):
            raise UnsupportedError(_PAGE_ACTOR_REFUSAL.format(uid=uid))

        logger.info("no conversation found; starting one from the profile page")
        opened, profile_name = self._start_from_profile(cdp, uid)
        if opened and self._wait_for_composer(cdp, timeout=20):
            return self._confirm_thread(
                cdp,
                uid,
                how="profile Message button",
                expected_name=profile_name,
            )

        raise ParseError(
            f"Messenger composer did not appear for {uid}; the recipient may not "
            "accept messages, the thread id may be wrong, or the page layout changed"
        )

    def _confirm_thread(
        self,
        cdp: BrowserTransport,
        uid: str,
        *,
        how: str,
        expected_name: str | None = None,
    ) -> tuple[str, str | None]:
        """Refuse to compose unless the open conversation belongs to ``uid``.

        A composer appearing is not proof of *whose* conversation it is: Facebook
        may redirect an unknown thread id, and a docked chat could otherwise
        supply the box. Evidence, strongest first: the thread id equals the
        recipient uid, or the composer's "Write to <name>" label matches the
        recipient's display name.
        """
        thread_id = self._thread_id(cdp)
        if thread_id is None:
            raise ParseError(
                f"Messenger opened no conversation for {uid} (via {how}); refusing to compose"
            )

        target = self._composer_target(cdp)
        if thread_id == uid:
            return thread_id, target

        expected = expected_name or self._recipient_name(uid)
        if expected and target and _same_name(target, expected):
            logger.info(
                "thread %s verified by name %r instead of uid %s (%s)",
                thread_id,
                target,
                uid,
                how,
            )
            return thread_id, target

        if expected:
            raise ParseError(
                f"refusing to send: the open conversation (thread {thread_id}, "
                f"addressed to {target!r}) does not match recipient {uid} "
                f"({expected!r}); opened via {how}, nothing was sent"
            )

        if how == "profile Message button":
            # We reached this box by clicking "Message" on profile.php?id=<uid>,
            # so the uid is verified even though a brand-new thread id is not.
            logger.warning(
                "thread %s for %s was verified only by the profile Message button "
                "(the recipient's name could not be read)",
                thread_id,
                uid,
            )
            return thread_id, target

        raise ParseError(
            f"refusing to send: thread {thread_id} (addressed to {target!r}) is not "
            f"recipient {uid} and its name could not be resolved to double-check; "
            f"opened via {how}, nothing was sent"
        )

    def _recipient_name(self, uid: str) -> str | None:
        """Display name of ``uid``, or ``None`` when it cannot be read.

        Only used on the ambiguous path (thread id != uid), where a public
        profile GET is cheaper than sending to the wrong person.
        """
        if self._profiles is None:
            return None
        try:
            profile = self._profiles.resolve(uid)
        except (FacebookError, ValueError) as exc:
            logger.debug("could not resolve %s for verification: %s", uid, exc)
            return None
        return profile.display_name

    def _acting_as_page(self, cdp: BrowserTransport) -> bool:
        """Whether the *live* session is switched into a Page identity.

        ``state.cookies`` is a snapshot taken when the jar was exported, so an
        identity switch performed in the browser afterwards would not show up
        there. The attached browser is therefore authoritative: ask it first and
        consult the snapshot only when it returns nothing usable (not attached,
        or an empty jar for this origin). Costs one round trip, and only on the
        path where the first two fallbacks already failed.
        """
        try:
            live = cdp.cookies()
        except TransportError as exc:
            logger.debug("could not read live cookies: %s", exc)
            live = {}

        if live:
            if live.get("i_user"):
                logger.debug(
                    "acting as a Page per the live browser (i_user); the exported "
                    "cookie jar does not necessarily include it"
                )
                return True
            if live.get("c_user"):
                # A live personal identity overrides a stale "Page" snapshot.
                return False

        return self._session.state.acting_as_page

    def _start_from_profile(self, cdp: BrowserTransport, uid: str) -> tuple[bool, str | None]:
        """Open the profile and click its "Message" button.

        Also returns the profile's display name, so the resulting composer can be
        checked against the person whose button was clicked (a brand-new thread
        gets an id that says nothing about the recipient).
        """
        self._pace()
        try:
            cdp.navigate(f"{PROFILE_BASE}{uid}", timeout=45.0)
        except TransportError as exc:
            logger.info("could not open profile page for %s: %s", uid, exc)
            return False, None

        deadline = time.monotonic() + 15
        name: str | None = None
        while time.monotonic() < deadline:
            if name is None:
                name = self._profile_name(cdp)
            position = self._message_button_position(cdp)
            if position:
                return self._click_at(cdp, position), name
            time.sleep(0.5)

        logger.info(
            "no Message button found on the profile of %s (a Page actor never gets "
            "one; otherwise the profile may not accept messages)",
            uid,
        )
        return False, None

    @staticmethod
    def _profile_name(cdp: BrowserTransport) -> str | None:
        """The profile page's heading, used to verify the composer it opens."""
        raw = cdp.evaluate(
            "(() => { const h = document.querySelector('h1'); "
            "return h ? (h.innerText || '').trim() : null; })()"
        )
        return str(raw).strip() if raw else None

    @staticmethod
    def _message_button_position(cdp: BrowserTransport) -> dict[str, float] | None:
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
    def _click_at(cdp: BrowserTransport, position: dict[str, float]) -> bool:
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
    def _thread_id(cdp: BrowserTransport) -> str | None:
        """Thread id from the current URL, or ``None`` when not in a thread.

        Returning the requested uid as a fallback (the old behaviour) made a
        redirect to the thread list look like a successfully opened conversation.
        """
        current = str(cdp.evaluate("location.href") or "")
        match = _THREAD_IN_URL.search(current)
        return match.group(1) if match else None

    @staticmethod
    def _composer_target(cdp: BrowserTransport) -> str | None:
        """Name from the composer's ``Write to <name>`` label."""
        raw = cdp.evaluate(_script("\n  return composerTarget;\n"))
        return str(raw).strip() if raw else None

    def _wait_for_composer(self, cdp: BrowserTransport, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._has_composer(cdp):
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _click_thread_row(cdp: BrowserTransport, uid: str) -> bool:
        clicked = cdp.evaluate(
            _script(
                rf"""
  const target = {uid!r};
  const threadId = (href) => /\/messages\/(?:e2ee\/)?t\/([0-9]+)\/?(?:[?#].*)?$/.exec(href);
  const link = [...document.querySelectorAll('a[href*="/messages/"]')]
    .find(a => ((threadId(a.getAttribute('href') || '') || [])[1]) === target);
  if (!link) return false;
  link.click();
  return true;
"""
            )
        )
        return bool(clicked)

    @staticmethod
    def _has_composer(cdp: BrowserTransport) -> bool:
        try:
            return bool(cdp.evaluate(_script("\n  return !!composer;\n")))
        except TransportError:
            return False

    # --- composing ------------------------------------------------------

    def _assert_composer_target(
        self, cdp: BrowserTransport, expected_target: str | None, *, when: str
    ) -> None:
        """Fail unless the composer still addresses ``expected_target``."""
        if not expected_target:
            return
        current_target = self._composer_target(cdp)
        if current_target and not _same_name(current_target, expected_target):
            raise FacebookError(
                f"the composer switched to {current_target!r} {when} "
                f"(expected {expected_target!r}); nothing was sent"
            )

    def _compose_and_send(
        self,
        cdp: BrowserTransport,
        text: str,
        *,
        expected_target: str | None = None,
    ) -> None:
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

        # The composer can be re-rendered between opening the thread and typing
        # (a notification opening a docked chat is enough). Re-read whose box it
        # is now; a different name means the conversation moved under us.
        self._assert_composer_target(cdp, expected_target, when="while composing")

        # insertText appends at the caret, so an unsent draft would be sent
        # together with our message ("old draft: hello"). Never touch it.
        draft = self._composer_text(cdp)
        if draft is None:
            raise ParseError("the Messenger composer disappeared while composing")
        if draft.strip():
            raise FacebookError(
                "the Messenger composer already contains an unsent draft "
                f"({draft[:80]!r}); nothing was sent. Clear it in the browser "
                "and retry."
            )

        cdp.send("Input.insertText", {"text": text})
        time.sleep(0.3)

        typed = self._composer_text(cdp) or ""
        if text[:20] not in typed:
            raise ParseError(
                f"composer did not accept the text (got {typed!r}); aborting before send"
            )

        # Only the text we produced counts, so another bubble's failure cannot be
        # mistaken for ours.
        needle = " ".join(text.split())[:20]
        failed_before = _failed_bubbles(cdp, needle)

        self._pace()

        # The conversation can move during the pacing wait (a docked chat opening
        # is enough), and the click lands wherever the composer points *now*.
        # Re-check immediately before sending, not only before typing.
        self._assert_composer_target(cdp, expected_target, when="before sending")

        # From here on the message may be on the wire. Anything that goes wrong
        # after this point is an *unobservable* outcome: reporting it as
        # "not_sent" would invite a duplicate resend.
        try:
            if not self._click_send(cdp):
                self._press_enter(cdp)

            cleared = self._wait_until_cleared(cdp)
            # A rejected message also clears the composer: Messenger moves the
            # text into a bubble and *then* stamps it "Couldn't send", so the
            # marker can appear seconds later. Only a *new* failure counts.
            if cleared and self._wait_for_rejection(cdp, failed_before, needle):
                raise SendRejectedError(
                    'Messenger marked the message as unsent ("Couldn\'t send"); it '
                    "did not reach the recipient — retrying is safe but will "
                    "probably fail the same way"
                )

            if not cleared:
                raise SendUncertainError(
                    "message may have been sent: the composer still contains text "
                    "after clicking send, so the outcome is unknown — check the "
                    "conversation before resending"
                )
        except (SendRejectedError, SendUncertainError):
            raise
        except FacebookError as exc:
            # e.g. a CDP read failing after the click: the send was issued, so the
            # outcome cannot be observed. Never downgrade this to "not_sent".
            raise SendUncertainError(
                "the send was issued but its outcome could not be observed "
                f"({exc}); check the conversation before resending"
            ) from exc

    def _wait_for_rejection(
        self,
        cdp: BrowserTransport,
        failed_before: int,
        needle: str | None = None,
        timeout: float = 4.0,
    ) -> bool:
        """Wait for a *new* unsent bubble; the marker lags the send by seconds.

        Live measurement: the bubble appears at once, but Messenger stamps it
        "Couldn't send" a second or two later, so a single check right after the
        composer clears misses it. Best-effort by nature: the marker is UI text.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _failed_bubbles(cdp, needle) > failed_before:
                return True
            time.sleep(0.4)
        return False

    @staticmethod
    def _composer_text(cdp: BrowserTransport) -> str | None:
        """Current composer text, or ``None`` when the composer is gone.

        The distinction matters for the delivery check: an empty string means
        "sent", ``None`` means the DOM changed underneath us and nothing can be
        concluded.
        """
        value: Any = cdp.evaluate(
            _script("\n  return composer ? (composer.innerText || '') : null;\n")
        )
        return None if value is None else str(value)

    @staticmethod
    def _click_send(cdp: BrowserTransport) -> bool:
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
    def _press_enter(cdp: BrowserTransport) -> None:
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

    def _wait_until_cleared(self, cdp: BrowserTransport, timeout: float = 15.0) -> bool:
        """True only when the composer still exists and is empty.

        A vanished composer is *not* success: a re-render or navigation also
        removes it, and silently reporting delivery would hide that.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self._composer_text(cdp)
            if current is None:
                raise SendUncertainError(
                    "the Messenger composer disappeared after sending; the outcome "
                    "is unknown (the message may still have been sent) — check the "
                    "conversation before resending"
                )
            if not current.strip():
                return True
            time.sleep(0.4)
        return False
