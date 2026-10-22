"""Exception hierarchy (L0 core layer).

All exceptions raised by upper layers derive from :class:`FacebookError`, so
callers (e.g. the MCP server layer) can catch a single type and map it to a
structured tool error.
"""

from __future__ import annotations


class FacebookError(Exception):
    """Base class for every facebook_mcp error."""

    code = "facebook_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message or self.code


class AuthError(FacebookError):
    """Session is missing or expired (cookie / fb_dtsg invalid)."""

    code = "auth_error"


class RateLimitError(FacebookError):
    """Request was rate limited."""

    code = "rate_limit"

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ChallengeError(FacebookError):
    """Facebook served a security challenge / CAPTCHA."""

    code = "challenge"


class NotFoundError(FacebookError):
    """The requested user / page does not exist."""

    code = "not_found"


class TransportError(FacebookError):
    """Network / transport layer failure."""

    code = "transport_error"


class HttpStatusError(TransportError):
    """Non-2xx HTTP response."""

    code = "http_status_error"

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ParseError(TransportError):
    """Response structure changed or could not be parsed."""

    code = "parse_error"


class SendUncertainError(FacebookError):
    """The send was issued but its outcome cannot be verified.

    Raised only after the UI was asked to send. Callers must **not** retry
    blindly: the message may already be in the conversation.
    """

    code = "send_uncertain"


class UnsupportedError(FacebookError):
    """The configured transport or account type cannot do this."""

    code = "unsupported"
