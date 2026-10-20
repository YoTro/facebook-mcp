"""Message domain models (L0 core layer)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Recipient(BaseModel):
    """A message recipient."""

    uid: str = Field(..., description="Numeric Facebook id of the recipient")
    name: str | None = Field(None, description="Display name")


class SendResult(BaseModel):
    """Result of the ``send_message`` tool."""

    ok: bool = Field(..., description="Whether the message was sent")
    thread_id: str | None = Field(None, description="Messenger thread id")
    message_id: str | None = Field(None, description="Sent message id")
    error: str | None = Field(None, description="Error message when ok=False")
