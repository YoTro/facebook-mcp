"""Message domain models (L0 core layer)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DeliveryState = Literal["not_sent", "submitted", "rejected", "unknown"]
"""What is known about a send attempt.

Two paths can deliver a message: the Lightspeed WebSocket (default) and the
CDP-driven UI (fallback). They can observe different amounts, so the state says
what is *known*, not which path ran.

* ``not_sent`` — nothing reached Messenger (validation, wrong conversation,
  composer missing/draft, or the channel refused before the frame was written).
  Safe to retry.
* ``submitted`` — Messenger accepted the message. Over the WebSocket the server's
  ``message_id`` comes back with it; over the UI fallback only the composer
  clearing is observable, so a retry may duplicate.
* ``rejected`` — the gateway explicitly marked it unsent
  (``markOptimisticMessageFailed``), so it never reached the recipient. Retrying
  is safe, though it will probably fail the same way. The UI fallback never
  claims this: the DOM cannot prove a marker belongs to our message.
* ``unknown`` — the send was issued but the outcome could not be observed (a lost
  reply, a dropped stream, a vanished composer, or an ambiguous UI failure
  marker). Check the conversation before resending.
"""


class SendResult(BaseModel):
    """Result of the ``send_message`` tool."""

    ok: bool = Field(..., description="Whether the message was submitted")
    delivery: DeliveryState = Field(
        ...,
        description=(
            "'not_sent' = nothing was submitted (safe to retry); 'submitted' = "
            "Messenger accepted it — the WebSocket path also returns the server's "
            "message_id, while the UI fallback only sees the composer clear, so a "
            "retry may duplicate; 'rejected' = the gateway explicitly refused it, so "
            "it never arrived (safe to retry); 'unknown' = the send was issued and the "
            "outcome could not be observed — check the conversation before resending"
        ),
    )
    thread_id: str | None = Field(None, description="Messenger thread id")
    message_id: str | None = Field(
        None, description="Server message id (available on the WebSocket path)"
    )
    error: str | None = Field(None, description="Error message when ok=False")
