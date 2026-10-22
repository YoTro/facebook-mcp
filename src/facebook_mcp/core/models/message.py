"""Message domain models (L0 core layer)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DeliveryState = Literal["not_sent", "submitted", "rejected", "unknown"]
"""What is known about a send attempt.

* ``not_sent`` — nothing reached Messenger (validation, wrong conversation,
  composer missing/draft/rejected the text). Safe to retry.
* ``submitted`` — the composer accepted the text and cleared it, i.e. the
  message was handed to Messenger. No server acknowledgement is readable from
  the web UI, so retrying may duplicate it.
* ``rejected`` — Messenger itself marked the message as unsent ("Couldn't
  send"), so it never reached the recipient. Retrying is safe, though it will
  probably fail the same way.
* ``unknown`` — send was clicked but the outcome could not be observed (the
  composer vanished or still holds text). Check the conversation first.
"""


class SendResult(BaseModel):
    """Result of the ``send_message`` tool."""

    ok: bool = Field(..., description="Whether the message was submitted")
    delivery: DeliveryState = Field(
        ...,
        description=(
            "'not_sent' = nothing was submitted (safe to retry); 'submitted' = the "
            "composer accepted and cleared the text, but no server acknowledgement is "
            "readable (retrying may duplicate); 'rejected' = Messenger marked it as "
            "unsent, so it never arrived (safe to retry); 'unknown' = send was issued "
            "and the outcome could not be verified"
        ),
    )
    thread_id: str | None = Field(None, description="Messenger thread id")
    message_id: str | None = Field(None, description="Sent message id")
    error: str | None = Field(None, description="Error message when ok=False")
