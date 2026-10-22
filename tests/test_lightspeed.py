"""Lightspeed frame codec, verified against a captured live frame.

The fixture is a real Messenger send captured from the page WebSocket. Every
captured identifier (thread, otid, epoch, trace) was replaced with an
equal-length placeholder, and the message text with a filler, so nothing
account-specific is committed while the byte layout stays exactly as observed.
If the codec and the fixture disagree, the wire format changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from facebook_mcp.transport import lightspeed as ls

FIXTURE = Path(__file__).parent / "fixtures" / "lightspeed_send_frame.hex"


@pytest.fixture(scope="module")
def frame_bytes() -> bytes:
    return bytes.fromhex(FIXTURE.read_text(encoding="utf-8").strip())


def test_split_real_frame(frame_bytes: bytes) -> None:
    frame = ls.split_frame(frame_bytes)
    assert frame.request_id == frame.request_id & 0xFFFF
    body = frame.json()
    assert body["app_id"] == ls.APP_ID
    assert body["type"] == 3


def test_rebuild_is_byte_identical(frame_bytes: bytes) -> None:
    frame = ls.split_frame(frame_bytes)
    assert ls.build_frame(frame.request_id, frame.body) == frame_bytes


def test_header_layout(frame_bytes: bytes) -> None:
    assert frame_bytes[0] == 0x0F
    assert frame_bytes[3:6] == b"\x02\x00\x00"
    assert frame_bytes[6:8] == b"{}"
    assert frame_bytes[8] == 0x0D
    assert frame_bytes[15] == 0x80
    declared = int.from_bytes(frame_bytes[11:15], "little")
    assert declared == len(frame_bytes) - ls.HEADER_LEN + ls.LENGTH_BIAS


def test_split_rejects_garbage() -> None:
    with pytest.raises(ls.FrameError):
        ls.split_frame(b"\x00" * 4)
    with pytest.raises(ls.FrameError):
        ls.split_frame(bytes([0x0A]) + b"\x00" * 20)


def test_length_mismatch_is_reported(frame_bytes: bytes) -> None:
    broken = bytearray(frame_bytes)
    broken[11:15] = (1).to_bytes(4, "little")
    with pytest.raises(ls.FrameError, match="length mismatch"):
        ls.split_frame(bytes(broken))


def test_build_send_body_matches_the_captured_shape(frame_bytes: bytes) -> None:
    captured = json.loads(ls.split_frame(frame_bytes).json()["payload"])
    captured_task = json.loads(captured["tasks"][0]["payload"])

    body = ls.build_send_body(
        thread_id=str(captured_task["thread_id"]),
        text="hello",
        otid="1234567890",
        request_id=7,
        version_id=captured["version_id"],
        epoch_id=captured["epoch_id"],
        data_trace_id=captured["data_trace_id"],
    )
    payload = json.loads(body["payload"])
    task = json.loads(payload["tasks"][0]["payload"])

    # Same keys as the live frame, so a shape drift is caught here.
    assert set(task) == set(captured_task)
    assert set(payload) == set(captured)
    assert set(body) == set(ls.split_frame(frame_bytes).json())
    assert task["text"] == "hello"
    assert task["thread_id"] == captured_task["thread_id"]
    assert payload["version_id"] == captured["version_id"]


def test_mark_read_task_is_optional() -> None:
    with_read = ls.build_send_body(thread_id="1", text="x", otid="o", request_id=1, version_id="v")
    without = ls.build_send_body(
        thread_id="1", text="x", otid="o", request_id=1, version_id="v", mark_read=False
    )
    assert len(json.loads(with_read["payload"])["tasks"]) == 2
    assert len(json.loads(without["payload"])["tasks"]) == 1


def test_encode_send_round_trips() -> None:
    body = ls.build_send_body(thread_id="1", text="hello", otid="o", request_id=9, version_id="v")
    raw = ls.encode_send(body, 9)
    decoded = ls.split_frame(raw)
    assert decoded.request_id == 9
    assert json.loads(decoded.json()["payload"])["version_id"] == "v"


def test_request_id_must_fit_u16() -> None:
    with pytest.raises(ls.FrameError):
        ls.build_frame(70000, b"{}")
