"""M5 tests: messaging client (browser path) with a scripted fake CDP transport."""

from __future__ import annotations

import pytest

from facebook_mcp.clients.message_client import MessageClient
from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import FacebookError, ParseError, UnsupportedError
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
        draft: str = "",
        vanish_after_send: bool = False,
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
        self.draft = draft
        self.vanish_after_send = vanish_after_send
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
            return {}
        if method == "Input.dispatchKeyEvent":
            self.key_events.append(str(params.get("type")))
            if self.clears_after_send:
                self.composer_text = ""
                self.sent = True
                if self.vanish_after_send:
                    self.composer_open = False
            return {}
        if method == "Input.dispatchMouseEvent":
            if params.get("type") == "mousePressed":
                self.mouse_clicks += 1
                self.composer_open = self.composer_after_profile
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
        if "return !!(composer || sendButton)" in expression:
            return self.composer_open
        if "sendButton.click()" in expression:
            if not self.send_button:
                return False
            if self.clears_after_send:
                self.composer_text = ""
                self.sent = True
                if self.vanish_after_send:
                    self.composer_open = False
            return True
        if "composer.innerText" in expression:
            return self.composer_text if self.composer_open else None
        return None


def _client(transport, **kwargs) -> MessageClient:
    session = SessionManager(Settings(cookie="c_user=1; xs=2", cookie_file=None))
    return MessageClient(transport, session, Settings(), **kwargs)


def _page_client(transport, **kwargs) -> MessageClient:
    """A client whose session is switched into a Page (``i_user`` present)."""
    session = SessionManager(
        Settings(cookie="c_user=100000000000001; xs=2; i_user=1", cookie_file=None)
    )
    assert session.state.acting_as_page is True
    return MessageClient(transport, session, Settings(), **kwargs)


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
    # The URL never became a thread, so the uid is reported back.
    assert result.thread_id == "100000000000003"


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
    cdp = FakeCdp(composer_on_thread=True, clears_after_send=False)
    with pytest.raises(FacebookError, match="may not have been delivered"):
        _client(cdp).send("200000000000001", "hi")


def test_empty_text_rejected() -> None:
    with pytest.raises(ValueError):
        _client(FakeCdp()).send("200000000000001", "   ")


def test_http_only_transport_is_rejected() -> None:
    transport = CurlCffiTransport(Settings(min_request_interval=0.0))
    try:
        client = _client(transport)
        with pytest.raises(UnsupportedError, match="needs a browser session"):
            client.send("200000000000001", "hi")
    finally:
        transport.close()


def test_graphql_path_is_documented_as_unavailable() -> None:
    client = _client(FakeCdp())
    with pytest.raises(UnsupportedError, match="service worker"):
        client.send_via_graphql("200000000000001", "hi")


# --- recipient resolution ----------------------------------------------


class _StubProfiles:
    def __init__(self, uid: str = "100000000000001") -> None:
        self.resolved: list[str] = []
        self._uid = uid

    def resolve(self, username: str) -> Profile:
        self.resolved.append(username)
        return Profile(uid=self._uid, username=username)


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


def test_mcp_tool_registered() -> None:
    import asyncio

    from facebook_mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "send_message" in names


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
    with pytest.raises(ParseError, match="disappeared after sending"):
        _client(cdp).send("200000000000001", "hello")
