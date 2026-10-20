"""Core infrastructure layer (L0).

Contains configuration, exceptions, logging and domain models. This layer must
not import anything from upper layers (transport/session/routes/clients/services).
"""

from .accounts import (
    DEFAULT_ACCOUNT,
    DEFAULT_HOME,
    AccountNameError,
    AccountPaths,
    list_accounts,
    resolve_home,
)
from .config import Settings, get_settings
from .exceptions import (
    AuthError,
    ChallengeError,
    FacebookError,
    HttpStatusError,
    NotFoundError,
    ParseError,
    RateLimitError,
    TransportError,
    UnsupportedError,
)
from .logger import get_logger

__all__ = [
    "DEFAULT_ACCOUNT",
    "DEFAULT_HOME",
    "AccountNameError",
    "AccountPaths",
    "AuthError",
    "ChallengeError",
    "FacebookError",
    "HttpStatusError",
    "NotFoundError",
    "ParseError",
    "RateLimitError",
    "Settings",
    "TransportError",
    "UnsupportedError",
    "get_logger",
    "get_settings",
    "list_accounts",
    "resolve_home",
]
