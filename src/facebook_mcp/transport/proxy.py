"""Proxy resolution (L1 helper).

Resolves the proxy used by the HTTP transport, in priority order:

1. explicit ``FB_PROXY`` setting
2. standard ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` env vars
3. macOS system proxy (``scutil --proxy``) when ``FB_PROXY_FROM_SYSTEM`` is on

Loopback targets always bypass the proxy (curl's ``noproxy``), so tests and
local Chrome debugging keep working while a proxy is configured.
"""

from __future__ import annotations

import os
import re
import subprocess

from ..core.logger import get_logger

logger = get_logger(__name__)

_HTTP_ENV_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)

# Loopback / link-local hosts that must never be proxied.
NO_PROXY_HOSTS = "localhost,127.0.0.1,::1,0.0.0.0,host.docker.internal"


def _from_env() -> str | None:
    for name in _HTTP_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def _from_macos_system() -> str | None:
    """Read the active proxy from macOS system settings."""
    try:
        out = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("could not read macOS proxy settings: %s", exc)
        return None

    if not out:
        return None

    def field(name: str) -> str | None:
        match = re.search(rf"^\s*{name}\s*:\s*(\S+)\s*$", out, re.MULTILINE)
        return match.group(1) if match else None

    def is_enabled(name: str) -> bool:
        return field(name) == "1"

    host = field("HTTPProxy")
    if is_enabled("HTTPSEnable"):
        port = field("HTTPSPort") or field("HTTPPort")
        if host and port:
            return f"http://{host}:{port}"

    if is_enabled("HTTPEnable"):
        port = field("HTTPPort")
        if host and port:
            return f"http://{host}:{port}"

    if is_enabled("SOCKSEnable"):
        socks_host = field("SOCKSProxy") or host
        socks_port = field("SOCKSPort")
        if socks_host and socks_port:
            return f"socks5h://{socks_host}:{socks_port}"

    return None


def resolve_proxy(explicit: str | None = None, use_system: bool = True) -> str | None:
    """Return the proxy URL to use, or ``None``."""
    if explicit:
        return explicit
    env_proxy = _from_env()
    if env_proxy:
        return env_proxy
    if use_system:
        return _from_macos_system()
    return None


def proxy_mapping(proxy: str | None) -> dict[str, str]:
    """Build the ``proxies`` mapping handed to curl_cffi."""
    if not proxy:
        return {}
    return {"http": proxy, "https": proxy}
