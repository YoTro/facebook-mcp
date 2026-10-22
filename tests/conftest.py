"""Pytest fixtures."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Ensure `src/` is importable when running pytest directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never let a test read or write the real ~/.facebook-mcp store."""
    monkeypatch.setenv("FB_HOME", str(tmp_path / "fb-home"))
    monkeypatch.delenv("FB_COOKIE", raising=False)
    monkeypatch.delenv("FB_COOKIE_FILE", raising=False)
    monkeypatch.delenv("FB_ACCOUNT", raising=False)

    from facebook_mcp.core.config import get_settings
    from facebook_mcp.services import factory
    from facebook_mcp.transport.throttle import reset_limiters

    reset_limiters()
    get_settings.cache_clear()
    factory._services_for.cache_clear()
    yield
    reset_limiters()
    get_settings.cache_clear()
    factory._services_for.cache_clear()
