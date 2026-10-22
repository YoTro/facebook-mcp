"""Lightspeed (Messenger sync) wire codec — L1.

Messenger does not send messages over HTTP. The page opens a WebSocket to
``wss://gateway.facebook.com/ws/lightspeed`` and posts JSON *tasks* inside a
fixed 16-byte binary header. This module encodes and decodes those frames so the
capture tools and any future protocol client share one implementation.

Layout (verified byte-for-byte against a real frame, see
``tests/test_lightspeed.py``)::

    off  size  value
    0    1     0x0f            frame type
    1    2     request id      u16 LE, repeated at offset 9
    3    3     0x02 0x00 0x00  flags
    6    2     "{}"            options JSON
    8    1     0x0d            body type
    9    2     request id      repeated
    11   4     u32 LE          len(body) + 2
    15   1     0x80            trailing flag
    16   n     body            UTF-8 JSON

Body shape::

    {"app_id": "...", "payload": "<json>", "request_id": 54, "type": 3}
      payload -> {"epoch_id": ..., "version_id": "...", "data_trace_id": "...",
                  "tasks": [{"label": "46", "queue_name": "<thread>",
                             "task_id": 1, "payload": "<json>"}]}
        task payload -> {"thread_id": ..., "otid": ..., "text": "...", ...}

``label`` 46 carries a message; 21 marks the thread read.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

HEADER_LEN = 16
FRAME_TYPE = 0x0F
BODY_TYPE = 0x0D
TRAILING_FLAG = 0x80
LENGTH_BIAS = 2
"""``u32`` length counts the trailing flag plus the body (observed on live frames)."""

APP_ID = "2220391788200892"
SEND_LABEL = "46"
READ_LABEL = "21"


class FrameError(ValueError):
    """The bytes are not a Lightspeed frame."""


@dataclass(frozen=True)
class Frame:
    """A decoded Lightspeed frame."""

    request_id: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def build_frame(request_id: int, body: bytes) -> bytes:
    """Encode ``body`` into the 16-byte header + payload frame."""
    if not 0 <= request_id <= 0xFFFF:
        raise FrameError(f"request_id {request_id} does not fit in u16")
    return (
        bytes([FRAME_TYPE])
        + struct.pack("<H", request_id)
        + bytes([0x02, 0x00, 0x00])
        + b"{}"
        + bytes([BODY_TYPE])
        + struct.pack("<H", request_id)
        + struct.pack("<I", len(body) + LENGTH_BIAS)
        + bytes([TRAILING_FLAG])
        + body
    )


def split_frame(raw: bytes) -> Frame:
    """Decode a frame captured from the socket."""
    if len(raw) < HEADER_LEN:
        raise FrameError(f"frame shorter than the {HEADER_LEN}-byte header")
    if raw[0] != FRAME_TYPE:
        raise FrameError(f"unexpected frame type {raw[0]:#04x}")
    request_id = struct.unpack("<H", raw[1:3])[0]
    declared = struct.unpack("<I", raw[11:15])[0]
    body = raw[HEADER_LEN:]
    if declared != len(body) + LENGTH_BIAS:
        raise FrameError(
            f"length mismatch: header says {declared}, body is {len(body)} (+{LENGTH_BIAS})"
        )
    if struct.unpack("<H", raw[9:11])[0] != request_id:
        raise FrameError("request id is not repeated at offset 9")
    return Frame(request_id=request_id, body=body)


def build_send_body(
    *,
    thread_id: str,
    text: str,
    otid: str,
    request_id: int,
    version_id: str,
    task_id: int = 1,
    epoch_id: int = 0,
    data_trace_id: str = "",
    mark_read: bool = True,
    last_read_watermark_ts: int = 0,
) -> dict[str, Any]:
    """Build the JSON envelope for a text message.

    Field-for-field equivalent to what Messenger sends (``source`` 65537,
    ``send_type`` 1, ``sync_group`` 1). ``version_id`` is the queue sync version
    the server expects and must be captured from a live session — a fresh
    connection cannot invent it.
    """
    message = {
        "thread_id": int(thread_id),
        "otid": otid,
        "source": 65537,
        "send_type": 1,
        "sync_group": 1,
        "mark_thread_read": 1 if mark_read else 0,
        "text": text,
        "initiating_source": 1,
        "skip_url_preview_gen": 0,
        "text_has_links": 1 if "http" in text else 0,
        "multitab_env": 0,
        "metadata_dataclass": '{"media_accessibility_metadata":{"alt_text":null}}',
    }
    tasks = [
        {
            "failure_count": None,
            "label": SEND_LABEL,
            "payload": json.dumps(message, separators=(",", ":")),
            "queue_name": thread_id,
            "task_id": task_id,
        }
    ]
    if mark_read:
        tasks.append(
            {
                "failure_count": None,
                "label": READ_LABEL,
                "payload": json.dumps(
                    {
                        "thread_id": int(thread_id),
                        "last_read_watermark_ts": last_read_watermark_ts,
                        "sync_group": 1,
                        "offline_threading_id": None,
                    },
                    separators=(",", ":"),
                ),
                "queue_name": thread_id,
                "task_id": task_id + 1,
            }
        )

    payload = {
        "epoch_id": epoch_id,
        "tasks": tasks,
        "version_id": version_id,
        "data_trace_id": data_trace_id,
    }
    return {
        "app_id": APP_ID,
        "payload": json.dumps(payload, separators=(",", ":")),
        "request_id": request_id,
        "type": 3,
    }


def encode_send(body: dict[str, Any], request_id: int) -> bytes:
    """Serialize a send envelope into a socket frame."""
    return build_frame(request_id, json.dumps(body, separators=(",", ":")).encode("utf-8"))


WARMUP_TASKS: tuple[tuple[str, str, str], ...] = (
    ("54", "insert_persistent_menu_ctas", "thread_key"),
    ("117", "insert_persistent_menu_items", "thread_key"),
    ("767", "fetch_custom_thread_commands", "thread_key"),
)
"""Thread-context fetches Messenger performs before it can send.

Verified by replay: a connection that posts task 46 without these gets
``markOptimisticMessageFailed / "Couldn't send."``; with them the server answers
``replaceOptimsiticMessage`` and a real ``mid.$…``. They are ordinary fetches
(menus, custom commands, contact profile questions), not session-bound
subscriptions, so they can be constructed from the thread key alone.
"""


def warmup_task_batches(thread_id: str, uid: str) -> list[list[dict[str, Any]]]:
    """Build the pre-send task batches, one envelope each."""
    key = int(thread_id)
    batches: list[list[dict[str, Any]]] = []
    task_id = 0
    for label, queue, field in WARMUP_TASKS:
        batches.append(
            [
                {
                    "failure_count": None,
                    "label": label,
                    "payload": json.dumps({field: key}, separators=(",", ":")),
                    "queue_name": queue,
                    "task_id": task_id,
                }
            ]
        )
        task_id += 1

    # Contact profile questions for both sides of the conversation.
    batches.append(
        [
            {
                "failure_count": None,
                "label": "207",
                "payload": json.dumps({"contact_id": contact}, separators=(",", ":")),
                "queue_name": "cpq_v2",
                "task_id": task_id + offset,
            }
            for offset, contact in enumerate((key, int(uid)))
        ]
    )
    return batches


def build_envelope(
    tasks: list[dict[str, Any]], *, request_id: int, version_id: str
) -> dict[str, Any]:
    """Wrap ``tasks`` in the payload/outer envelope (epoch is per send)."""
    import random as _random

    payload = {
        "epoch_id": _random.getrandbits(63),
        "tasks": tasks,
        "version_id": version_id,
    }
    return {
        "app_id": APP_ID,
        "payload": json.dumps(payload, separators=(",", ":")),
        "request_id": request_id,
        "type": 3,
    }
