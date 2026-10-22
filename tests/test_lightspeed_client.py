"""Protocol sender: frame building and verdict parsing, with a fake gateway.

No network here — the real gateway was used once during development to confirm
the sequence; these tests pin the pieces that must not regress.
"""

from __future__ import annotations

import json

import pytest

from facebook_mcp.clients.lightspeed_client import (
    DEFAULT_VERSION_ID,
    LightspeedError,
    LightspeedSender,
    gateway_url,
    parse_ack,
)
from facebook_mcp.core.config import Settings
from facebook_mcp.core.exceptions import SendRejectedError
from facebook_mcp.session.manager import SessionManager
from facebook_mcp.transport import lightspeed as ls


class _FakeWs:
    """Replays one scripted verdict whenever it sees our otid."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.sent: list[bytes] = []
        self.closed = False
        self._pending: list[str] = []

    def send_binary(self, data: bytes) -> None:
        self.sent.append(data)
        text = data[16:].decode("utf-8", "replace")
        try:
            payload = json.loads(json.loads(text)["payload"])
        except Exception:
            return
        for task in payload.get("tasks", []):
            inner = json.loads(task["payload"])
            if task.get("label") == "46":
                self._pending.append(self.verdict.replace("<otid>", inner["otid"]))

    def recv(self) -> str:
        if not self._pending:
            raise RuntimeError("stream closed")
        return self._pending.pop(0)

    def close(self) -> None:
        self.closed = True


def _sender(verdict: str) -> tuple[LightspeedSender, _FakeWs]:
    fake = _FakeWs(verdict)
    session = SessionManager(Settings(cookie="c_user=42; xs=tok", cookie_file=None))
    sender = LightspeedSender(Settings(), session)
    sender._connect = lambda uid, cookies: fake  # type: ignore[method-assign]
    return sender, fake


ACCEPT = '{"step":[1,[4,[5,"replaceOptimsiticMessage","<otid>","mid.$cTEST"]]]}'
REJECT = '{"step":[1,[4,[5,"markOptimisticMessageFailed","<otid>","Couldn\'t send.",false]]]}'


def test_send_delivers_and_returns_message_id() -> None:
    sender, fake = _sender(ACCEPT)
    ack = sender.send("1234567890", "hello")
    assert ack.message_id == "mid.$cTEST"
    assert ack.rejected is False
    assert fake.closed is True


def _labels(fake: _FakeWs) -> list[str]:
    labels: list[str] = []
    for frame in fake.sent:
        body = json.loads(ls.split_frame(frame).json()["payload"])
        labels.extend(task["label"] for task in body["tasks"])
    return labels


def test_send_does_not_warm_up_by_default() -> None:
    """A public page conversation accepts a bare task 46 (measured)."""
    sender, fake = _sender(ACCEPT)
    sender.send("1234567890", "hello")
    assert _labels(fake) == ["46", "21"]


def test_warmup_is_available_for_threads_that_need_it() -> None:
    fake = _FakeWs(ACCEPT)
    session = SessionManager(Settings(cookie="c_user=42; xs=tok", cookie_file=None))
    sender = LightspeedSender(Settings(), session, warmup=True)
    sender._connect = lambda uid, cookies: fake  # type: ignore[method-assign]

    sender.send("1234567890", "hello")
    assert _labels(fake) == ["54", "117", "767", "207", "207", "46", "21"]


def test_send_raises_on_refusal() -> None:
    sender, _ = _sender(REJECT)
    with pytest.raises(SendRejectedError, match="did not reach"):
        sender.send("1234567890", "hello")


def test_send_without_a_session_raises_auth_error() -> None:
    """The channel goes through require_auth, so a missing session is AuthError."""
    from facebook_mcp.core.exceptions import AuthError

    session = SessionManager(Settings(cookie=None, cookie_file=None, cdp_cookie_bootstrap=False))
    sender = LightspeedSender(Settings(cdp_cookie_bootstrap=False), session)
    with pytest.raises(AuthError):
        sender.send("1", "hello")


def test_verdict_without_message_id_is_uncertain_not_success() -> None:
    """Accepted but unprovable: the frame is already on the wire."""
    from facebook_mcp.core.exceptions import SendUncertainError

    sender, _ = _sender('{"step":[1,[4,[5,"replaceOptimsiticMessage","<otid>",null]]]}')
    with pytest.raises(SendUncertainError, match="did not return a message id"):
        sender.send("1", "hello")


def test_parse_ack_accept_and_reject() -> None:
    accepted = parse_ack('"replaceOptimsiticMessage","123","mid.$cX"', "123")
    assert accepted.message_id == "mid.$cX"
    assert accepted.rejected is False

    rejected = parse_ack('"markOptimisticMessageFailed","123","Couldn\'t send."', "123")
    assert rejected.rejected is True


def test_gateway_url_carries_cookie_auth() -> None:
    url = gateway_url("42")
    assert url.startswith("wss://gateway.facebook.com/ws/lightspeed?")
    assert "x-dgw-authtype=1:0" in url
    assert "x-dgw-uuid=42" in url
    assert url.count("x-dgw-loggingid=") == 1


def test_warmup_batches_target_the_thread_and_self() -> None:
    batches = ls.warmup_task_batches("999", "42")
    assert len(batches) == 4
    contacts = [json.loads(t["payload"])["contact_id"] for t in batches[3]]
    assert contacts == [999, 42]


def test_default_version_is_stable() -> None:
    """It was identical across threads and sessions, so it is not queue state."""
    assert DEFAULT_VERSION_ID == "39529440880004510"


def test_accept_is_parsed_from_the_escaped_nested_payload() -> None:
    """The real frame wraps its steps in a JSON string (escaped quotes)."""
    frame = (
        '{"payload":"{\\"step\\":[1,[4,[5,\\"replaceOptimsiticMessage\\",'
        '\\"690029728476153161\\",\\"mid.$cAAP90DZ5z4q\\"]]]}"}'
    )
    ack = parse_ack(frame, "690029728476153161")
    assert ack.rejected is False
    assert ack.message_id == "mid.$cAAP90DZ5z4q"


def test_reject_is_parsed_from_the_escaped_nested_payload() -> None:
    frame = (
        '{"payload":"{\\"step\\":[1,[4,[5,\\"markOptimisticMessageFailed\\",'
        '\\"690029728476153161\\",\\"Couldn\'t send.\\",false]]]}"}'
    )
    assert parse_ack(frame, "690029728476153161").rejected is True


# --- verdict attribution, post-write uncertainty, shared infrastructure --


def test_foreign_rejection_in_the_same_frame_does_not_taint_our_send() -> None:
    """A frame may carry several messages; only our own step counts."""
    sender, _ = _sender(
        '{"step":['
        '[5,"markOptimisticMessageFailed","999","Couldn\'t send.",false],'
        '[5,"replaceOptimsiticMessage","<otid>","mid.$cOURS"]]}'
    )
    ack = sender.send("1", "hello")
    assert ack.message_id == "mid.$cOURS"
    assert ack.rejected is False


def test_foreign_failure_without_our_verdict_times_out_as_uncertain() -> None:
    """Our otid appears, but no verdict step for it: keep waiting, then unknown."""
    from facebook_mcp.core.exceptions import SendUncertainError

    foreign = (
        '{"step":[[5,"markOptimisticMessageFailed","999","Couldn\'t send.",false],'
        '["note","<otid>"]]}'
    )
    sender, fake = _sender(foreign)
    # Keep replaying the unrelated frame until the reply window closes.
    fake.recv = lambda: foreign.replace("<otid>", fake_otid[0])  # type: ignore[method-assign]
    fake_otid: list[str] = []

    original_send_binary = fake.send_binary

    def capture(data: bytes) -> None:
        original_send_binary(data)
        text = data[16:].decode("utf-8", "replace")
        try:
            payload = json.loads(json.loads(text)["payload"])
            for task in payload.get("tasks", []):
                inner = json.loads(task["payload"])
                if task.get("label") == "46":
                    fake_otid.append(inner["otid"])
        except Exception:
            pass
        fake._pending.clear()  # the canned verdict is replaced by `foreign` above

    fake.send_binary = capture  # type: ignore[method-assign]

    sender._reply_timeout = 0.2
    with pytest.raises(SendUncertainError, match="did not acknowledge"):
        sender.send("1", "hello")


def test_lost_reply_after_write_is_uncertain_not_not_sent() -> None:
    """A closed stream after the frame was written must not invite a resend."""
    from facebook_mcp.core.exceptions import SendUncertainError

    sender, fake = _sender(ACCEPT)
    fake.recv = lambda: (_ for _ in ()).throw(RuntimeError("socket closed"))  # type: ignore[method-assign]
    with pytest.raises(SendUncertainError, match="closed before acknowledging"):
        sender.send("1", "hello")


def test_write_failure_is_uncertain() -> None:
    from facebook_mcp.core.exceptions import SendUncertainError

    sender, fake = _sender(ACCEPT)

    def boom(_data: bytes) -> None:
        raise OSError("pipe broken")

    fake.send_binary = boom  # type: ignore[method-assign]
    with pytest.raises(SendUncertainError, match="could not be confirmed as written"):
        sender.send("1", "hello")


def test_send_draws_on_the_shared_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """One send = one slot in the account's budget, like HTTP and the UI path."""
    import facebook_mcp.clients.lightspeed_client as lc

    waits: list[int] = []

    class _Limiter:
        def wait(self) -> None:
            waits.append(1)

    monkeypatch.setattr(lc, "limiter_for", lambda settings: _Limiter())
    sender, _ = _sender(ACCEPT)
    sender.send("1", "hello")
    assert waits == [1]


def test_bootstrap_happens_before_sending(tmp_path, monkeypatch) -> None:
    """An empty store with a working provider must still send."""
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    from facebook_mcp.core.config import get_settings

    get_settings.cache_clear()
    session = SessionManager(
        Settings(cookie=None, cookie_file=None), lambda: {"c_user": "5", "xs": "t"}
    )
    fake = _FakeWs(ACCEPT)
    sender = LightspeedSender(Settings(), session)
    sender._connect = lambda uid, cookies: fake  # type: ignore[method-assign]

    sent_uid: list[str] = []
    sender._connect = lambda uid, cookies: (sent_uid.append(uid), fake)[1]  # type: ignore[method-assign]

    ack = sender.send("1", "hello")
    assert sent_uid == ["5"], "the bootstrapped identity was not used"
    assert ack.message_id == "mid.$cTEST"
    get_settings.cache_clear()


def test_proxy_configuration_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    sender, _ = _sender(ACCEPT)
    sender._settings = Settings(proxy="http://127.0.0.1:10808")
    options = sender._proxy_options()
    assert options["http_proxy_host"] == "127.0.0.1"
    assert options["http_proxy_port"] == 10808
    assert options["proxy_type"] == "http"


def test_socks_proxy_needs_python_socks() -> None:
    """websocket-client talks SOCKS through python-socks, not PySocks."""
    sender, _ = _sender(ACCEPT)
    sender._settings = Settings(proxy="socks5h://127.0.0.1:1080")
    try:
        import python_socks  # noqa: F401
    except ImportError:
        with pytest.raises(LightspeedError, match="python-socks"):
            sender._proxy_options()
    else:  # pragma: no cover - only when python-socks is installed
        # The scheme travels verbatim: socks5h resolves names at the proxy.
        assert sender._proxy_options()["proxy_type"] == "socks5h"


def test_proxy_credentials_are_forwarded() -> None:
    sender, _ = _sender(ACCEPT)
    sender._settings = Settings(proxy="http://user:p%40ss@127.0.0.1:3128")
    options = sender._proxy_options()
    assert options["http_proxy_host"] == "127.0.0.1"
    assert options["http_proxy_port"] == 3128
    assert options["http_proxy_auth"] == ("user", "p@ss")


def test_http_proxy_scheme_is_kept() -> None:
    sender, _ = _sender(ACCEPT)
    sender._settings = Settings(proxy="https://127.0.0.1:8443")
    assert sender._proxy_options()["proxy_type"] == "https"


def test_user_agent_override_is_used() -> None:
    sender, _ = _sender(ACCEPT)
    sender._settings = Settings(user_agent="custom-agent/1")
    assert (sender._settings.user_agent or "") == "custom-agent/1"


# --- ack parsing must decode, not match text -----------------------------


def test_accept_with_spaces_after_commas_is_parsed() -> None:
    """JSON writers may put whitespace after commas; a text match misses it."""
    frame = json.dumps(
        {"payload": json.dumps({"step": [[5, "replaceOptimsiticMessage", "123", "mid.$cX"]]})}
    )
    ack = parse_ack(frame, "123")
    assert ack.matched is True
    assert ack.message_id == "mid.$cX"
    assert ack.rejected is False


def test_marker_inside_a_normal_string_is_not_a_rejection() -> None:
    """A message body quoting the marker must not condemn the send."""
    frame = json.dumps(
        {
            "payload": json.dumps(
                {
                    "step": [[5, "replaceOptimsiticMessage", "123", "mid.$cY"]],
                    "note": 'markOptimisticMessageFailed"123',
                }
            )
        }
    )
    ack = parse_ack(frame, "123")
    assert ack.rejected is False
    assert ack.message_id == "mid.$cY"


def test_rejection_is_read_from_the_step_not_the_text() -> None:
    frame = json.dumps(
        {
            "payload": json.dumps(
                {"step": [[5, "markOptimisticMessageFailed", "123", "Couldn't send."]]}
            )
        }
    )
    ack = parse_ack(frame, "123")
    assert ack.matched is True
    assert ack.rejected is True


def test_unrelated_step_is_not_a_verdict() -> None:
    frame = json.dumps(
        {"payload": json.dumps({"step": [[5, "replaceOptimsiticMessage", "999", "mid.$cZ"]]})}
    )
    ack = parse_ack(frame, "123")
    assert ack.matched is False


def test_undecodable_frame_falls_back_but_stays_anchored() -> None:
    raw = 'garbage "markOptimisticMessageFailed" , "123" trailing'
    assert parse_ack(raw, "123").rejected is True
    assert parse_ack(raw, "999").matched is False


# --- handshake auth failure invalidates the session ---------------------


def test_rejected_handshake_invalidates_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from facebook_mcp.core.exceptions import AuthError

    class _BadHandshake(Exception):
        status_code = 401

    def boom(*args: object, **kwargs: object) -> None:
        raise _BadHandshake("Handshake status 401 Unauthorized")

    import websocket

    monkeypatch.setattr(websocket, "create_connection", boom)

    session = SessionManager(Settings(cookie="c_user=42; xs=tok", cookie_file=None))
    sender = LightspeedSender(Settings(), session)
    with pytest.raises(AuthError, match="refused the credentials"):
        sender.send("1", "hello")

    assert session.rejected, "an auth rejection left the session marked as valid"


def test_transport_failure_does_not_invalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("network unreachable")

    import websocket

    monkeypatch.setattr(websocket, "create_connection", boom)

    session = SessionManager(Settings(cookie="c_user=42; xs=tok", cookie_file=None))
    sender = LightspeedSender(Settings(), session)
    with pytest.raises(LightspeedError):
        sender.send("1", "hello")
    assert session.rejected is None
