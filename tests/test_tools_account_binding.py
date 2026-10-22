"""Operational tools must act on the account they were told to use.

``--account`` used to pick only the *storage* directory: the transport fell back
to the global ``FB_CDP_URL``, so exporting cookies for "work" could read the
"default" browser's jar and write it under the wrong name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    monkeypatch.setenv("FB_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.delenv("FB_ACCOUNT", raising=False)

    accounts = tmp_path / "accounts"
    for name, port in (("default", 9222), ("work", 9333)):
        directory = accounts / name
        directory.mkdir(parents=True)
        (directory / "account.json").write_text(
            json.dumps({"cdp_url": f"http://127.0.0.1:{port}"}), encoding="utf-8"
        )

    from facebook_mcp.core.config import get_settings
    from facebook_mcp.services import factory

    get_settings.cache_clear()
    factory._services_for.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    factory._services_for.cache_clear()


def test_build_settings_uses_the_account_cdp_url(home: Path) -> None:
    from facebook_mcp.services.factory import build_settings

    assert build_settings("default").cdp_url.endswith(":9222")
    assert build_settings("work").cdp_url.endswith(":9333")


def test_export_cookies_attaches_to_the_selected_account(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import export_cookies

    seen: list[str] = []

    class _StubTransport:
        def __init__(self, settings) -> None:
            seen.append(settings.cdp_url)

        def cookies(self, url: str | None = None) -> dict[str, str]:
            return {"c_user": "42", "xs": "tok"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(export_cookies, "CdpTransport", _StubTransport)

    assert export_cookies.main(["--account", "work"]) == 0
    assert seen == ["http://127.0.0.1:9333"]

    written = home / "accounts" / "work" / "cookies.txt"
    assert written.is_file()
    assert "c_user\t42" in written.read_text(encoding="utf-8")


def test_export_cookies_default_matches_the_global_setting(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import export_cookies

    seen: list[str] = []

    class _StubTransport:
        def __init__(self, settings) -> None:
            seen.append(settings.cdp_url)

        def cookies(self, url: str | None = None) -> dict[str, str]:
            return {"c_user": "1", "xs": "2"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(export_cookies, "CdpTransport", _StubTransport)
    assert export_cookies.main([]) == 0
    assert seen == ["http://127.0.0.1:9222"]


def test_sniff_attaches_to_the_selected_account(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sniff

    seen: list[str] = []

    class _StubTransport:
        def __init__(self, settings) -> None:
            seen.append(settings.cdp_url)

        def send(self, method: str, params: dict | None = None) -> dict:
            raise RuntimeError("stop: we only care about the binding")

        def close(self) -> None:
            pass

    monkeypatch.setattr(sniff, "CdpTransport", _StubTransport)
    monkeypatch.setattr(sys, "stderr", open("/dev/null", "w"))  # noqa: SIM115

    try:
        sniff.main(["--account", "work", "--seconds", "0"])
    finally:
        sys.stderr = sys.__stderr__

    assert seen == ["http://127.0.0.1:9333"]


def test_export_cookies_honours_home_for_browser_selection(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--home selects the credential root; the browser must follow it."""
    import export_cookies

    other = tmp_path / "other-home"
    work = other / "accounts" / "work"
    work.mkdir(parents=True)
    (work / "account.json").write_text(
        json.dumps({"cdp_url": "http://127.0.0.1:9444"}), encoding="utf-8"
    )

    seen: list[str] = []

    class _StubTransport:
        def __init__(self, settings) -> None:
            seen.append(settings.cdp_url)

        def cookies(self, url: str | None = None) -> dict[str, str]:
            return {"c_user": "7", "xs": "t"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(export_cookies, "CdpTransport", _StubTransport)

    assert export_cookies.main(["--account", "work", "--home", str(other)]) == 0
    assert seen == ["http://127.0.0.1:9444"], "the global home chose the browser"
    assert (work / "cookies.txt").is_file()
