"""Messaging use cases (L4)."""

from __future__ import annotations

from ..clients.message_client import MessageClient
from ..core.exceptions import FacebookError
from ..core.logger import get_logger
from ..core.models import SendResult

logger = get_logger(__name__)


class MessageService:
    """Sends Messenger messages on behalf of the session owner."""

    def __init__(self, message_client: MessageClient) -> None:
        self._messages = message_client

    def send_message(self, recipient: str, text: str) -> SendResult:
        """Send ``text`` to ``recipient``; never raises for expected failures."""
        try:
            result = self._messages.send(recipient, text)
        except FacebookError as exc:
            logger.warning("send_message failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        except ValueError as exc:
            return SendResult(ok=False, error=str(exc))

        logger.info("message sent to %s (thread %s)", recipient, result.thread_id)
        return result
