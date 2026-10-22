"""M5 tests: messaging client (browser path) with a scripted fake CDP transport."""

from __future__ import annotations

import pytest

from facebook_mcp.clients.message_client import MessageClient
from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import (
    FacebookError,
    ParseError,
    SendUncertainError,
    UnsupportedError,
)
from facebook_mcp.core.models import Profile
from facebook_mcp.session.manager import SessionManager
from facebook_mcp.transport.factory import HybridTransport
from facebook_mcp.transport.http import CurlCffiTransport


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the client's polling loops run instantly during tests."""
    import facebook_mcp.clients.message_client as mc

    clock = {"now": 1000.0}

    def monotonic() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(mc.time, "monotonic", monotonic)
    monkeypatch.setattr(mc.time, "sleep", sleep)


class FakeCdp:
    """Scripted stand-in for CdpTransport.

    Behaves like a tiny state machine: navigation can "open" a composer, the
    profile page can expose a Message button, and sending clears the box.
    """

    def __init__(
        self,
        *,
        composer_on_thread: bool = True,
        composer_after_row_click: bool = False,
        composer_after_profile: bool = False,
        accepts_text: bool = True,
        clears_after_send: bool = True,
        send_button: bool = True,
        thread_row: bool = True,
        message_button: bool = True,
        live_cookies: dict[str, str] | None = None,
        composer_target: str | None = None,
        redirect_thread: str | None = None,
        send_fails: bool = False,
        existing_failures: int = 0,
        draft: str = "",
        vanish_after_send: bool = False,
        switch_target_after_insert: str | None = None,
        clear_target_after_insert: bool = False,
        vanish_before_click: bool = False,
        url: str = "https://www.facebook.com/messages/t/200000000000001/",
    ) -> None:
        self.composer_on_thread = composer_on_thread
        self.composer_after_row_click = composer_after_row_click
        self.composer_after_profile = composer_after_profile
        self.accepts_text = accepts_text
        self.clears_after_send = clears_after_send
        self.send_button = send_button
        self.thread_row = thread_row
        self.message_button = message_button
        self.live_cookies = live_cookies or {}
        self.composer_target = composer_target
        self.redirect_thread = redirect_thread
        self.send_fails = send_fails
        self.existing_failures = existing_failures
        self.send_failed = False
        self.draft = draft
        self.vanish_after_send = vanish_after_send
        self.switch_target_after_insert = switch_target_after_insert
        self.clear_target_after_insert = clear_target_after_insert
        self.vanish_before_click = vanish_before_click
        self.url = url

        self.composer_open = False
        self.composer_text = ""
        self.sent = False
        self.navigations: list[str] = []
        self.inserted: list[str] = []
        self.key_events: list[str] = []
        self.mouse_clicks = 0

    @property
    def name(self) -> str:
        return "fake-cdp"

    # --- transport surface ---

    def navigate(self, url: str, timeout: float | None = None) -> None:
        self.navigations.append(url)
        if "/messages/t/" in url:
            if self.redirect_thread:
                # Facebook sometimes answers /messages/t/<uid>/ with a different
                # conversation; the client must notice.
                self.url = f"https://www.facebook.com/messages/t/{self.redirect_thread}/"
            else:
                self.url = url
            self.composer_open = self.composer_on_thread
            self.composer_text = self.draft
        elif "profile.php" in url:
            self.url = url
            self.composer_open = False

    def cookies(self, url: str | None = None) -> dict[str, str]:
        return dict(self.live_cookies)

    def request(self, req):  # pragma: no cover - unused
        raise AssertionError("not used")

    def close(self) -> None:
        pass

    def send(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        if method == "Input.insertText":
            self.inserted.append(params.get("text", ""))
            if self.accepts_text:
                self.composer_text = params.get("text", "")
                self.composer_open = True
            if self.switch_target_after_insert:
                # e.g. a docked chat stole the box during the pacing wait
                self.composer_target = self.switch_target_after_insert
            if self.clear_target_after_insert:
                # the label became unreadable (re-render, collapsed chip, ...)
                self.composer_target = None
            return {}
        if method == "Input.dispatchKeyEvent":
            self.key_events.append(str(params.get("type")))
            if self.clears_after_send:
                self.composer_text = ""
                self.sent = True
                if self.send_fails:
                    self.send_failed = True
                if self.vanish_after_send:
                    self.composer_open = False
            return {}
        if method == "Input.dispatchMouseEvent":
            if params.get("type") == "mousePressed":
                self.mouse_clicks += 1
                self.composer_open = self.composer_after_profile
                if self.composer_after_profile:
                    # Clicking "Message" opens a brand-new thread, whose id is
                    # not the recipient's uid.
                    self.url = "https://www.facebook.com/messages/t/300000000000001/"
            return {}
        return {}

    # --- JS evaluation: dispatched on distinctive markers ---

    def evaluate(self, expression: str, **kwargs):
        if "location.href" in expression:
            return self.url
        if "composer.focus()" in expression:
            return "ok" if self.composer_open else "missing"
        if "getBoundingClientRect" in expression:
            if self.message_button:
                return {"x": 100.0, "y": 200.0}
            return None
        if 'href*="/messages/"' in expression:
            if not self.thread_row:
                return False
            self.composer_open = self.composer_after_row_click
            return True
        if "return !!composer" in expression:
            return self.composer_open
        if "sendButton.click()" in expression:
            if self.vanish_before_click:
                # e.g. the box was replaced between typing and clicking
                self.composer_open = False
            if not self.send_button:
                return False
            if self.clears_after_send:
                self.composer_text = ""
                self.sent = True
                if self.send_fails:
                    self.send_failed = True
                if self.vanish_after_send:
                    self.composer_open = False
            return True
        if "const markers =" in expression:
            return self.existing_failures + (1 if self.send_failed else 0)
        if "return composerTarget" in expression:
            return self.composer_target
        if "composer.innerText" in expression:
            return self.composer_text if self.composer_open else None
        return None


def _client(transport, **kwargs) -> MessageClient:
    # message_transport="ui": these tests exercise the DOM path and must never
    # reach the gateway (tests are offline by contract).
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    return MessageClient(transport, session, Settings(message_transport="ui"), **kwargs)


def _page_client(transport, **kwargs) -> MessageClient:
    """A client whose session is switched into a Page (``i_user`` present)."""
    session = SessionManager(
        Settings(cookie="c_user=100000000000001; xs=2; i_user=1", cookie_file=None)
    )
    assert session.state.acting_as_page is True
    return MessageClient(transport, session, Settings(message_transport="ui"), **kwargs)


# --- happy path ---------------------------------------------------------


def test_send_success_via_button() -> None:
    cdp = FakeCdp(composer_on_thread=True)
    result = _client(cdp).send("200000000000001", "hello there")
    assert result.ok is True
    assert result.thread_id == "200000000000001"
    assert cdp.navigations == ["https://www.facebook.com/messages/t/200000000000001/"]
    assert cdp.inserted == ["hello there"]
    assert cdp.sent is True


def test_send_falls_back_to_enter() -> None:
    cdp = FakeCdp(composer_on_thread=True, send_button=False)
    result = _client(cdp).send("200000000000001", "hi")
    assert result.ok is True
    assert cdp.key_events == ["keyDown", "char", "keyUp"]


def test_send_falls_back_to_thread_row() -> None:
    cdp = FakeCdp(composer_on_thread=False, composer_after_row_click=True)
    result = _client(cdp).send("200000000000001", "hi")
    assert result.ok is True
    assert cdp.sent is True


def test_send_starts_new_conversation_from_profile() -> None:
    """No existing thread: profile → Message button → composer."""
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        composer_after_profile=True,
    )
    result = _client(cdp).send("100000000000003", "hi")
    assert result.ok is True
    assert cdp.navigations[0] == "https://www.facebook.com/messages/t/100000000000003/"
    assert cdp.navigations[-1] == "https://www.facebook.com/profile.php?id=100000000000003"
    assert cdp.mouse_clicks == 1
    # A brand-new conversation gets its own thread id, not the recipient's uid.
    assert result.thread_id == "300000000000001"


# --- failure modes ------------------------------------------------------


def test_missing_composer_raises() -> None:
    cdp = FakeCdp(composer_on_thread=False, composer_after_row_click=False)
    with pytest.raises(ParseError, match="composer did not appear"):
        _client(cdp).send("200000000000001", "hi")


def test_no_message_button_on_profile_raises() -> None:
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        message_button=False,
    )
    with pytest.raises(ParseError, match="composer did not appear"):
        _client(cdp).send("200000000000001", "hi")


# --- page actors --------------------------------------------------------
#
# A Page cannot start a conversation from a profile: Facebook does not render
# the "Message" button for it. Detected from the i_user cookie, so the client
# refuses instead of polling for a button that will never appear.


def test_page_actor_refuses_profile_fallback() -> None:
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        message_button=False,
    )
    with pytest.raises(UnsupportedError, match="acting as a Page"):
        _page_client(cdp).send("100000000000003", "hi")

    # It gave up before opening the profile page (and before the 35s of polling).
    assert cdp.navigations == ["https://www.facebook.com/messages/t/100000000000003/"]
    assert cdp.mouse_clicks == 0
    assert cdp.inserted == []


def test_page_actor_can_reply_in_existing_thread() -> None:
    cdp = FakeCdp(composer_on_thread=True)
    result = _page_client(cdp).send("100000000000003", "hi")
    assert result.ok is True
    assert cdp.sent is True


def test_page_actor_can_use_thread_row() -> None:
    cdp = FakeCdp(composer_on_thread=False, composer_after_row_click=True)
    result = _page_client(cdp).send("100000000000003", "hi")
    assert result.ok is True
    assert cdp.sent is True


def test_personal_account_still_uses_profile_fallback() -> None:
    """The refusal must not leak into personal sessions."""
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        composer_after_profile=True,
    )
    result = _client(cdp).send("100000000000003", "hi")
    assert result.ok is True
    assert cdp.navigations[-1] == "https://www.facebook.com/profile.php?id=100000000000003"
    assert cdp.mouse_clicks == 1


def test_page_actor_detected_from_live_browser_cookies() -> None:
    """The exported jar is a snapshot; the browser knows the current identity."""
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        live_cookies={"c_user": "100000000000001", "i_user": "1"},
    )
    with pytest.raises(UnsupportedError, match="acting as a Page"):
        _client(cdp).send("100000000000003", "hi")
    assert cdp.navigations == ["https://www.facebook.com/messages/t/100000000000003/"]


def test_live_cookies_without_i_user_are_not_a_page() -> None:
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        composer_after_profile=True,
        live_cookies={"c_user": "100000000000001", "xs": "2"},
    )
    assert _client(cdp).send("100000000000003", "hi").ok is True
    assert cdp.mouse_clicks == 1


def test_acting_as_page_comes_from_i_user_cookie() -> None:
    personal = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    assert personal.state.impersonator_id is None
    assert personal.state.acting_as_page is False

    page = SessionManager(Settings(cookie="c_user=9; xs=2; i_user=1", cookie_file=None))
    assert page.state.impersonator_id == "1"
    assert page.state.acting_as_page is True


def test_composer_rejects_text_aborts_before_send() -> None:
    cdp = FakeCdp(composer_on_thread=True, accepts_text=False)
    with pytest.raises(ParseError, match="did not accept the text"):
        _client(cdp).send("200000000000001", "hi")
    assert cdp.sent is False


def test_not_cleared_reports_delivery_doubt() -> None:
    """The click happened but the box still holds text: outcome unknown."""
    cdp = FakeCdp(composer_on_thread=True, clears_after_send=False)
    with pytest.raises(SendUncertainError, match="may have been sent"):
        _client(cdp).send("200000000000001", "hi")


def test_empty_text_rejected() -> None:
    with pytest.raises(ValueError):
        _client(FakeCdp()).send("200000000000001", "   ")


def test_ui_mode_without_a_browser_is_rejected() -> None:
    """With message_transport="ui" the UI is the only path, so no browser = error."""
    transport = CurlCffiTransport(Settings(min_request_interval=0.0))
    try:
        client = _client(transport)
        with pytest.raises(UnsupportedError, match="needs a browser session"):
            client.send("200000000000001", "hi")
    finally:
        transport.close()


def test_graphql_mutation_is_an_explicit_dead_end() -> None:
    """There is no page-level GraphQL send; the protocol lives on the WebSocket."""
    client = _client(FakeCdp())
    with pytest.raises(UnsupportedError, match="Lightspeed WebSocket"):
        client.send_via_graphql("200000000000001", "hi")


# --- recipient resolution ----------------------------------------------


class _StubProfiles:
    def __init__(self, uid: str = "100000000000001", name: str | None = None) -> None:
        self.resolved: list[str] = []
        self._uid = uid
        self._name = name

    def resolve(self, username: str) -> Profile:
        self.resolved.append(username)
        return Profile(uid=self._uid, username=username, display_name=self._name)


def test_recipient_numeric_passthrough() -> None:
    profiles = _StubProfiles()
    client = _client(FakeCdp(), profile_client=profiles)
    assert client.resolve_recipient("123456") == "123456"
    assert profiles.resolved == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("exampleuser", "exampleuser"),
        ("@exampleuser", "exampleuser"),
        ("https://www.facebook.com/exampleuser", "exampleuser"),
        ("https://www.facebook.com/exampleuser/", "exampleuser"),
        ("facebook.com/exampleuser?ref=bookmarks", "exampleuser"),
    ],
)
def test_recipient_username_normalisation(value: str, expected: str) -> None:
    profiles = _StubProfiles()
    client = _client(FakeCdp(), profile_client=profiles)
    assert client.resolve_recipient(value) == "100000000000001"
    assert profiles.resolved == [expected]


def test_recipient_url_with_numeric_id() -> None:
    profiles = _StubProfiles()
    client = _client(FakeCdp(), profile_client=profiles)
    assert client.resolve_recipient("https://www.facebook.com/profile.php?id=999") == "999"
    assert profiles.resolved == []


def test_recipient_without_profile_client() -> None:
    client = _client(FakeCdp())
    with pytest.raises(UnsupportedError, match="numeric uid"):
        client.resolve_recipient("exampleuser")


def test_recipient_empty() -> None:
    with pytest.raises(ValueError):
        _client(FakeCdp()).resolve_recipient("  ")


# --- transport helper ---------------------------------------------------


def test_cdp_from_hybrid() -> None:
    from facebook_mcp.transport.browsing import cdp_from

    hybrid = HybridTransport(Settings())
    assert cdp_from(hybrid) is not None
    assert cdp_from(CurlCffiTransport(Settings(min_request_interval=0.0))) is None


# --- service behaviour --------------------------------------------------


def test_service_returns_structured_error() -> None:
    from facebook_mcp.services.message_service import MessageService

    cdp = FakeCdp(composer_on_thread=False, composer_after_row_click=False)
    service = MessageService(_client(cdp))
    result = service.send_message("200000000000001", "hi")
    assert result.ok is False
    assert "composer did not appear" in (result.error or "")


def test_service_returns_ok() -> None:
    from facebook_mcp.services.message_service import MessageService

    service = MessageService(_client(FakeCdp()))
    result = service.send_message("200000000000001", "hi")
    assert result.ok is True
    assert result.delivery == "submitted"


# --- live browser wins over a stale snapshot ----------------------------
#
# The jar is a snapshot: it can still say "Page" after the browser was switched
# back to the personal identity. The attached browser is authoritative, so a
# live personal session must not be blocked by the stale snapshot.


def test_stale_page_snapshot_does_not_block_live_personal_session() -> None:
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        composer_after_profile=True,
        live_cookies={"c_user": "1"},  # live: personal, no i_user
    )
    result = _page_client(cdp).send("100000000000003", "hi")
    assert result.ok is True
    # It went all the way to the profile fallback instead of refusing.
    assert cdp.navigations[-1].startswith("https://www.facebook.com/profile.php?id=")
    assert cdp.mouse_clicks == 1


def test_empty_live_jar_falls_back_to_the_snapshot() -> None:
    """An unreachable/empty live jar must not silently allow a Page."""
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        composer_after_profile=True,
        live_cookies={},
    )
    with pytest.raises(UnsupportedError, match="acting as a Page"):
        _page_client(cdp).send("100000000000003", "hi")


def test_live_page_overrides_personal_snapshot() -> None:
    cdp = FakeCdp(
        composer_on_thread=False,
        composer_after_row_click=False,
        live_cookies={"c_user": "1", "i_user": "9"},
    )
    with pytest.raises(UnsupportedError, match="acting as a Page"):
        _client(cdp).send("100000000000003", "hi")


# --- the composer is a shared, stateful input ---------------------------


def test_existing_draft_is_never_concatenated() -> None:
    """insertText appends at the caret: an unsent draft would be sent too."""
    cdp = FakeCdp(composer_on_thread=True, draft="old draft: ")
    with pytest.raises(FacebookError, match="unsent draft"):
        _client(cdp).send("200000000000001", "hello")

    assert cdp.inserted == []
    assert cdp.sent is False
    assert cdp.composer_text == "old draft: "


def test_whitespace_only_draft_is_not_a_draft() -> None:
    cdp = FakeCdp(composer_on_thread=True, draft="   \n")
    result = _client(cdp).send("200000000000001", "hello")
    assert result.ok is True


def test_vanished_composer_is_not_success() -> None:
    """A re-render removes the composer too; that is not proof of delivery."""
    cdp = FakeCdp(composer_on_thread=True, vanish_after_send=True)
    with pytest.raises(SendUncertainError, match="disappeared after sending"):
        _client(cdp).send("200000000000001", "hello")


# --- recipient verification ---------------------------------------------


def test_refuses_when_facebook_redirects_to_another_thread() -> None:
    """A composer answering for a different conversation must never be used."""
    cdp = FakeCdp(composer_on_thread=True, redirect_thread="200000000000999")
    with pytest.raises(ParseError, match="refusing to send"):
        _client(cdp).send("200000000000001", "hi")
    assert cdp.inserted == [], "text was typed into an unverified conversation"
    assert cdp.sent is False


def test_refuses_when_the_composer_belongs_to_someone_else() -> None:
    """The 'Write to <name>' label is checked against the recipient's name."""
    cdp = FakeCdp(
        composer_on_thread=True,
        redirect_thread="200000000000999",
        composer_target="Bob",
    )
    profiles = _StubProfiles(uid="200000000000001", name="Alice")
    with pytest.raises(ParseError, match="does not match recipient"):
        _client(cdp, profile_client=profiles).send("200000000000001", "hi")
    assert cdp.sent is False


def test_accepts_a_redirect_when_the_name_matches() -> None:
    """New conversations legitimately have a thread id that is not the uid."""
    cdp = FakeCdp(
        composer_on_thread=True,
        redirect_thread="300000000000001",
        composer_target="Alice Smith",
    )
    profiles = _StubProfiles(uid="200000000000001", name="Alice Smith")
    result = _client(cdp, profile_client=profiles).send("200000000000001", "hi")
    assert result.ok is True
    assert result.thread_id == "300000000000001"


def test_thread_row_matching_is_exact_not_substring() -> None:
    cdp = FakeCdp(composer_on_thread=False, composer_after_row_click=True)
    client = _client(cdp)
    # The row matcher is JavaScript, so assert the expression the client sends.
    captured: list[str] = []
    cdp.evaluate = lambda expression, **kw: (captured.append(expression), True)[1]
    client._click_thread_row(cdp, "123")
    assert any("threadId(" in expr for expr in captured)
    assert any("== target" in expr for expr in captured)


# --- delivery states ----------------------------------------------------


def test_uncertain_send_is_reported_as_unknown() -> None:
    from facebook_mcp.services.message_service import MessageService

    cdp = FakeCdp(composer_on_thread=True, clears_after_send=False)
    result = MessageService(_client(cdp)).send_message("200000000000001", "hi")
    assert result.ok is False
    assert result.delivery == "unknown"
    assert "may have been sent" in (result.error or "")


def test_failure_before_the_click_is_reported_as_not_sent() -> None:
    from facebook_mcp.services.message_service import MessageService

    cdp = FakeCdp(composer_on_thread=False, composer_after_row_click=False)
    result = MessageService(_client(cdp)).send_message("200000000000001", "hi")
    assert result.ok is False
    assert result.delivery == "not_sent"
    assert cdp.sent is False


def test_ui_failure_marker_is_reported_as_unknown() -> None:
    """Messenger clears the composer and then marks the bubble "Couldn't send".

    The UI path cannot prove the label belongs to *our* message (a message whose
    own text is exactly that label looks identical), and calling it "rejected"
    would tell the caller a retry is safe. Only the gateway may say rejected.
    """
    from facebook_mcp.services.message_service import MessageService

    cdp = FakeCdp(composer_on_thread=True, send_fails=True)
    result = MessageService(_client(cdp)).send_message("200000000000001", "hi")

    assert result.ok is False
    assert result.delivery == "unknown"
    assert "cannot prove" in (result.error or "")
    assert result.thread_id == "200000000000001"
    assert cdp.send_failed is True


def test_an_older_failed_bubble_does_not_poison_a_later_send() -> None:
    """Only *new* failures count, otherwise one bad message taints the thread."""
    from facebook_mcp.services.message_service import MessageService

    cdp = FakeCdp(composer_on_thread=True, existing_failures=1)
    result = MessageService(_client(cdp)).send_message("200000000000001", "hi")

    assert result.ok is True
    assert result.delivery == "submitted"


# --- failure classification and recipient verification -------------------


class _RaisingCdp(FakeCdp):
    """Fails on a chosen JS evaluation, optionally only after the send click."""

    def __init__(self, *, raise_on: str, after_send_only: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.raise_on = raise_on
        self.after_send_only = after_send_only

    def evaluate(self, expression: str, **kwargs):
        if self.raise_on in expression and (self.sent or not self.after_send_only):
            from facebook_mcp.transport.cdp import CdpError

            raise CdpError("DevTools connection reset")
        return super().evaluate(expression, **kwargs)


def test_error_after_the_click_is_unknown_not_not_sent() -> None:
    """A lost reply after dispatch must not look safe to retry."""
    from facebook_mcp.services.message_service import MessageService

    # The composer read after the click explodes.
    cdp = _RaisingCdp(raise_on="composer.innerText", after_send_only=True, composer_on_thread=True)
    service = MessageService(_client(cdp))

    result = service.send_message("200000000000001", "hello")

    assert result.ok is False
    assert result.delivery == "unknown"
    assert "could not be observed" in (result.error or "")


def test_error_before_the_click_stays_not_sent() -> None:
    """A failure while typing never reached the wire."""
    from facebook_mcp.services.message_service import MessageService

    cdp = _RaisingCdp(raise_on="composer.focus()", composer_on_thread=True)
    result = MessageService(_client(cdp)).send_message("200000000000001", "hello")

    assert result.delivery == "not_sent"


def test_failure_detection_ignores_message_body_text() -> None:
    """Sending text that mentions the marker must not read as a rejection."""
    seen: list[str] = []

    class _CapturingCdp(FakeCdp):
        def evaluate(self, expression: str, **kwargs):
            if "const markers =" in expression:
                seen.append(expression)
            return super().evaluate(expression, **kwargs)

    cdp = _CapturingCdp(composer_on_thread=True)
    result = _client(cdp).send("200000000000001", "fixed the Couldn't send issue")

    assert result.ok is True
    assert result.delivery == "submitted"
    # The generated probe compares whole leaf labels and scopes them to our text;
    # a substring search over article bodies is what caused the false alarm.
    assert seen, "rejection probe was not executed"
    assert "childElementCount" in seen[0]
    assert "includes(text)" in seen[0] or "markers.includes" in seen[0]
    assert "fixed the Couldn" in seen[0]


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ("Example User", "Example User Two"),
        ("Example User Two", "Example User"),
        ("Alice Sm", "Alice Smith"),  # no truncation marker: not the same
        ("Alice", "Alice Smith"),  # too short to judge
    ],
)
def test_names_that_are_only_prefixes_do_not_match(actual: str, expected: str) -> None:
    from facebook_mcp.clients.message_client import _same_name

    assert _same_name(actual, expected) is False


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ("Example User", "Example User"),
        ("Alice Sm\u2026", "Alice Smith"),
        ("Alice Sm...", "Alice Smith"),
        ("alice  smith", "Alice Smith"),
    ],
)
def test_truncated_or_equal_names_match(actual: str, expected: str) -> None:
    from facebook_mcp.clients.message_client import _same_name

    assert _same_name(actual, expected) is True


def test_conversation_switch_after_typing_is_caught_before_click() -> None:
    """Typing and clicking are separated by the pacing wait; re-check in between."""
    cdp = FakeCdp(
        composer_on_thread=True,
        composer_target="Example User",
        switch_target_after_insert="Example User Two",
    )

    with pytest.raises(FacebookError, match="switched to"):
        _client(cdp).send("200000000000001", "hello")

    assert cdp.sent is False, "the message was sent to the wrong conversation"


# --- normalisation and "cannot verify" -----------------------------------


def test_multiline_text_is_matched_against_a_normalised_bubble() -> None:
    """A message with line breaks must still be attributable to its bubble."""
    seen: list[str] = []

    class _CapturingCdp(FakeCdp):
        def evaluate(self, expression: str, **kwargs):
            if "const markers =" in expression:
                seen.append(expression)
            return super().evaluate(expression, **kwargs)

    from facebook_mcp.services.message_service import MessageService

    cdp = _CapturingCdp(composer_on_thread=True, send_fails=True)
    result = MessageService(_client(cdp)).send_message("200000000000001", "Hello\nExample User")

    assert seen, "rejection probe was not executed"
    script = seen[0]
    # The needle is collapsed to one line...
    assert "Hello Example User" in script
    # ...and the bubble text is collapsed the same way before comparing.
    assert "\\\\s+/g" in script or "\\s+/g" in script
    # A failure marker was produced, but the UI cannot attribute it with
    # certainty, so the result must be "unknown" — never "rejected", which would
    # tell the caller a retry is safe.
    assert result.delivery == "unknown"


def test_unreadable_composer_target_blocks_the_send() -> None:
    """An expected recipient that can no longer be read is not 'verified'."""
    cdp = FakeCdp(
        composer_on_thread=True,
        composer_target="Example User",
        clear_target_after_insert=True,
    )

    with pytest.raises(FacebookError, match="cannot verify the conversation recipient"):
        _client(cdp).send("200000000000001", "hello")
    assert cdp.sent is False


def test_enter_fallback_is_not_pressed_when_the_composer_vanished() -> None:
    """Keystrokes must not land on an unrelated element."""
    cdp = FakeCdp(
        composer_on_thread=True,
        composer_target="Example User",
        send_button=False,
        vanish_before_click=True,
    )

    with pytest.raises(FacebookError, match="composer disappeared before"):
        _client(cdp).send("200000000000001", "hello")

    assert cdp.key_events == [], "Enter was pressed with no composer"
    assert cdp.sent is False


# --- protocol rejection is disambiguated, not silently retried ----------


class _RejectingSender:
    def send(self, uid: str, text: str):
        from facebook_mcp.core.exceptions import SendRejectedError

        raise SendRejectedError("Messenger refused the message")


def test_protocol_rejection_falls_back_to_the_ui_in_auto_mode() -> None:
    """A refusal means nothing was sent, so the UI can safely disambiguate."""
    cdp = FakeCdp(composer_on_thread=True)
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = MessageClient(cdp, session, Settings(message_transport="auto"))
    client._lightspeed = _RejectingSender()  # type: ignore[assignment]

    result = client.send("200000000000001", "hello")

    assert result.ok is True
    assert cdp.sent is True, "the UI path was not tried after a protocol refusal"


def test_protocol_rejection_is_final_in_lightspeed_mode() -> None:
    from facebook_mcp.core.exceptions import SendRejectedError

    cdp = FakeCdp(composer_on_thread=True)
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    client = MessageClient(cdp, session, Settings(message_transport="lightspeed"))
    client._lightspeed = _RejectingSender()  # type: ignore[assignment]

    with pytest.raises(SendRejectedError):
        client.send("200000000000001", "hello")
    assert cdp.sent is False
