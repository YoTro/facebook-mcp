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


def test_send_without_c_user_is_rejected() -> None:
    session = SessionManager(Settings(cookie=None, cookie_file=None, cdp_cookie_bootstrap=False))
    sender = LightspeedSender(Settings(), session)
    with pytest.raises(LightspeedError, match="no c_user"):
        sender.send("1", "hello")


def test_verdict_without_message_id_is_not_success() -> None:
    sender, _ = _sender('{"step":[1,[4,[5,"replaceOptimsiticMessage","<otid>",null]]]}')
    with pytest.raises(LightspeedError, match="without confirming"):
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
