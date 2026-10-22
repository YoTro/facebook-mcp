"""Messaging use cases (L4)."""

from __future__ import annotations

from ..clients.message_client import MessageClient
from ..core.exceptions import FacebookError, SendRejectedError, SendUncertainError
from ..core.logger import get_logger
from ..core.models import SendResult

logger = get_logger(__name__)


class MessageService:
    """Sends Messenger messages on behalf of the session owner."""

    def __init__(self, message_client: MessageClient) -> None:
        self._messages = message_client

    def send_message(self, recipient: str, text: str) -> SendResult:
        """Send ``text`` to ``recipient``; never raises for expected failures.

        ``delivery`` tells the caller whether a retry is safe: anything before the
        send click means ``not_sent``, while an unobservable outcome after it is
        ``unknown`` and must not be retried blindly.
        """
        try:
            result = self._messages.send(recipient, text)
        except SendUncertainError as exc:
            logger.warning("send_message outcome unknown: %s", exc)
            return SendResult(ok=False, delivery="unknown", error=str(exc))
        except SendRejectedError as exc:
            logger.warning("send_message rejected by Messenger: %s", exc)
            return SendResult(
                ok=False,
                delivery="rejected",
                thread_id=exc.thread_id,
                error=str(exc),
            )
        except FacebookError as exc:
            logger.warning("send_message failed: %s", exc)
            return SendResult(ok=False, delivery="not_sent", error=str(exc))
        except ValueError as exc:
            return SendResult(ok=False, delivery="not_sent", error=str(exc))

        logger.info("message sent to %s (thread %s)", recipient, result.thread_id)
        return result
