"""Account isolation tests: credential layout, resolution order, multi-account."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from facebook_mcp.core.accounts import (
    AccountNameError,
    AccountPaths,
    list_accounts,
    resolve_home,
    validate_account_name,
)
from facebook_mcp.core.config import Settings
from facebook_mcp.session.manager import SessionManager

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t123\n"
    ".facebook.com\tTRUE\t/\tTRUE\t0\txs\ttok\n"
)


# --- name validation -----------------------------------------------------


@pytest.mark.parametrize("name", ["default", "work", "acct_2", "a.b-c", "A1"])
def test_valid_account_names(name: str) -> None:
    assert validate_account_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", " ", "../escape", "a/b", ".hidden", "-dash", "x" * 65, "with space", "semi;colon"],
)
def test_invalid_account_names(name: str) -> None:
    with pytest.raises(AccountNameError):
        validate_account_name(name)


def test_account_paths_rejects_bad_name() -> None:
    with pytest.raises(AccountNameError):
        AccountPaths(home=Path("/tmp"), name="../x")


# --- home resolution -----------------------------------------------------


def test_resolve_home_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FB_HOME", raising=False)
    assert resolve_home() == Path.home() / ".facebook-mcp"


def test_resolve_home_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    assert resolve_home() == tmp_path


def test_resolve_home_explicit_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path / "env"))
    assert resolve_home(tmp_path / "explicit") == tmp_path / "explicit"


# --- layout & permissions ------------------------------------------------


def test_layout_paths(tmp_path: Path) -> None:
    paths = AccountPaths.for_account("work", tmp_path)
    assert paths.directory == tmp_path / "accounts" / "work"
    assert paths.cookies_file == paths.directory / "cookies.txt"
    assert paths.meta_file == paths.directory / "account.json"


def test_ensure_creates_private_dirs(tmp_path: Path) -> None:
    paths = AccountPaths.for_account("work", tmp_path).ensure()
    assert paths.directory.is_dir()
    assert stat.S_IMODE(paths.directory.stat().st_mode) == 0o700


def test_meta_roundtrip(tmp_path: Path) -> None:
    paths = AccountPaths.for_account("work", tmp_path)
    assert paths.load_meta() == {}
    paths.merge_meta(c_user="42", cdp_url="http://127.0.0.1:9223")
    meta = paths.load_meta()
    assert meta["c_user"] == "42"
    assert meta["cdp_url"] == "http://127.0.0.1:9223"
    assert stat.S_IMODE(paths.meta_file.stat().st_mode) == 0o600


def test_permission_warning(tmp_path: Path) -> None:
    paths = AccountPaths.for_account("work", tmp_path).ensure()
    paths.cookies_file.write_text(NETSCAPE)
    paths.cookies_file.chmod(0o644)
    assert paths.permission_warning() is not None

    paths.cookies_file.chmod(0o600)
    assert paths.permission_warning() is None


def test_list_accounts(tmp_path: Path) -> None:
    assert list_accounts(tmp_path) == []
    AccountPaths.for_account("default", tmp_path).ensure()
    AccountPaths.for_account("work", tmp_path).ensure()
    (tmp_path / "accounts" / "not-an-account.txt").write_text("x")
    assert list_accounts(tmp_path) == ["default", "work"]


# --- cookie resolution order --------------------------------------------


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        account="default",
        home=str(tmp_path),
        cookie=None,
        cookie_file=None,
        cdp_cookie_bootstrap=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_account_store_is_used_by_default(tmp_path: Path) -> None:
    paths = AccountPaths.for_account("default", tmp_path).ensure()
    paths.cookies_file.write_text(NETSCAPE, encoding="utf-8")

    manager = SessionManager(_settings(tmp_path))
    assert manager.is_authenticated is True
    assert manager.state.user_id == "123"
    assert manager.from_config is True


def test_explicit_cookie_file_wins(tmp_path: Path) -> None:
    store = AccountPaths.for_account("default", tmp_path).ensure()
    store.cookies_file.write_text(NETSCAPE.replace("c_user\t123", "c_user\t999"), encoding="utf-8")

    explicit = tmp_path / "explicit.txt"
    explicit.write_text(NETSCAPE.replace("c_user\t123", "c_user\t111"), encoding="utf-8")

    manager = SessionManager(_settings(tmp_path, cookie_file=str(explicit)))
    assert manager.state.user_id == "111"


def test_env_cookie_is_last_resort(tmp_path: Path) -> None:
    manager = SessionManager(_settings(tmp_path, cookie="c_user=777; xs=t"))
    assert manager.state.user_id == "777"
    assert manager.from_config is True


def test_no_cookies_is_anonymous(tmp_path: Path) -> None:
    manager = SessionManager(_settings(tmp_path))
    assert manager.is_authenticated is False
    assert manager.from_config is False


# --- multi-account ------------------------------------------------------


def test_accounts_are_isolated(tmp_path: Path) -> None:
    for name, uid in (("default", "111"), ("work", "222")):
        paths = AccountPaths.for_account(name, tmp_path).ensure()
        paths.cookies_file.write_text(
            NETSCAPE.replace("c_user\t123", f"c_user\t{uid}"), encoding="utf-8"
        )

    first = SessionManager(_settings(tmp_path, account="default"))
    second = SessionManager(_settings(tmp_path, account="work"))

    assert first.state.user_id == "111"
    assert second.state.user_id == "222"


def test_settings_account_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path, account="work")
    assert settings.account_paths().name == "work"
    assert settings.account_paths().home == tmp_path


def test_build_settings_applies_account_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    paths = AccountPaths.for_account("work", tmp_path).ensure()
    paths.merge_meta(cdp_url="http://127.0.0.1:9333")

    settings = factory.build_settings("work")
    assert settings.account == "work"
    assert settings.cdp_url == "http://127.0.0.1:9333"


def test_get_services_returns_distinct_instances_per_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    a = factory.get_services("default")
    b = factory.get_services("work")
    try:
        assert a is not b
        assert a.account == "default"
        assert b.account == "work"
        # Same account returns the cached instance.
        assert factory.get_services("default") is a
    finally:
        a.close()
        b.close()
        factory._services_for.cache_clear()


# --- MCP surface --------------------------------------------------------


def test_account_argument_reaches_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    import facebook_mcp.server as server
    from facebook_mcp.core.models import PostTimeResult
    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    seen: list[str] = []

    class StubPost:
        def get_latest_post_time(self, username: str) -> PostTimeResult:
            seen.append(username)
            return PostTimeResult(ok=True, username=username)

    class Stub:
        post = StubPost()

    monkeypatch.setattr(server, "get_services", lambda account=None: Stub())

    result = server.get_user_latest_post_time("someone", account="work")
    assert result.ok is True
    assert seen == ["someone"]


def test_global_credentials_do_not_leak_into_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``account="work"`` must not silently borrow the global identity.

    FB_COOKIE / FB_COOKIE_FILE describe whoever FB_ACCOUNT points at, and a
    cookie file outranks the account store — inheriting them would make the
    "isolated" account directory a lie.
    """
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    monkeypatch.setenv("FB_ACCOUNT", "default")
    monkeypatch.setenv("FB_COOKIE_FILE", str(tmp_path / "default-only.txt"))
    monkeypatch.setenv("FB_COOKIE", "c_user=1; xs=2")

    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    assert factory.build_settings(None).cookie_file == str(tmp_path / "default-only.txt")
    assert factory.build_settings("default").cookie == "c_user=1; xs=2"

    work = factory.build_settings("work")
    assert work.account == "work"
    assert work.cookie is None
    assert work.cookie_file is None
    assert work.account_paths().name == "work"


def test_another_account_uses_its_own_cookie_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    monkeypatch.setenv("FB_ACCOUNT", "default")
    monkeypatch.setenv("FB_COOKIE_FILE", str(tmp_path / "default-only.txt"))

    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    paths = AccountPaths.for_account("work", tmp_path).ensure()
    paths.cookies_file.write_text(NETSCAPE.replace("123", "222"), encoding="utf-8")

    settings = factory.build_settings("work")
    session = SessionManager(settings)
    assert session.state.user_id == "222"


def test_foreign_account_does_not_inherit_the_global_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FB_CDP_URL belongs to FB_ACCOUNT; borrowing it would leak its identity.

    The auto transport bootstraps cookies from whatever browser cdp_url points
    at, so a foreign account without its own binding must have no browser at all.
    """
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    monkeypatch.setenv("FB_CDP_URL", "http://127.0.0.1:9222")
    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    AccountPaths.for_account("work", tmp_path).ensure()

    assert factory.build_settings(None).cdp_url == "http://127.0.0.1:9222"
    assert factory.build_settings("work").cdp_url is None, (
        "account 'work' inherited the default account's browser"
    )


def test_own_cdp_url_still_wins_for_a_foreign_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    from facebook_mcp.services import factory

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    AccountPaths.for_account("work", tmp_path).merge_meta(cdp_url="http://127.0.0.1:9333")
    assert factory.build_settings("work").cdp_url == "http://127.0.0.1:9333"


def test_unbound_browser_fails_with_an_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FB_HOME", str(tmp_path))
    from facebook_mcp.services import factory
    from facebook_mcp.transport.cdp import CdpError, CdpTransport

    factory.get_settings.cache_clear()
    factory._services_for.cache_clear()

    settings = factory.build_settings("work")
    transport = CdpTransport(settings)
    with pytest.raises(CdpError, match="no browser is bound to account 'work'"):
        transport.cookies()
    transport.close() if hasattr(transport, "close") else None


# --- browser-bootstrapped sessions become HTTP-capable -------------------


def test_bootstrap_persists_cookies_for_the_next_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = SessionManager(settings, lambda: {"c_user": "7", "xs": "tok", "datr": "d"})
    assert manager.require_auth().user_id == "7"

    paths = AccountPaths.for_account("default", tmp_path)
    assert paths.cookies_file.is_file()
    assert stat.S_IMODE(paths.cookies_file.stat().st_mode) == 0o600
    assert "c_user\t7" in paths.cookies_file.read_text(encoding="utf-8")
    assert paths.load_meta()["source"] == "cdp-bootstrap"

    # A later process reads the store and needs no browser at all.
    reopened = SessionManager(_settings(tmp_path))
    assert reopened.is_authenticated is True
    assert reopened.state.user_id == "7"
    assert reopened.from_config is True


def test_bootstrap_does_not_shadow_a_user_managed_jar(tmp_path: Path) -> None:
    explicit = tmp_path / "mine.txt"
    explicit.write_text(NETSCAPE, encoding="utf-8")
    settings = _settings(tmp_path, cookie_file=str(explicit))

    SessionManager(settings)  # loads the user's jar
    assert not AccountPaths.for_account("default", tmp_path).cookies_file.exists()


def test_invalidate_discards_a_bootstrapped_jar(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = SessionManager(settings, lambda: {"c_user": "7", "xs": "tok"})
    manager.require_auth()

    paths = AccountPaths.for_account("default", tmp_path)
    assert paths.cookies_file.is_file()

    manager.invalidate("HTTP 403")
    assert not paths.cookies_file.exists(), "a refused jar would be reloaded next start"
    assert paths.load_meta()["source"] == "invalidated"


def test_invalidate_keeps_a_user_managed_jar(tmp_path: Path) -> None:
    paths = AccountPaths.for_account("default", tmp_path).ensure()
    paths.cookies_file.write_text(NETSCAPE, encoding="utf-8")  # no bootstrap marker

    manager = SessionManager(_settings(tmp_path))
    assert manager.is_authenticated is True

    manager.invalidate("HTTP 403")
    assert paths.cookies_file.exists(), "the user's own jar must not be deleted"


def test_invalidate_does_not_touch_a_jar_the_user_switched_to(tmp_path: Path) -> None:
    """A store written by bootstrap must not invalidate an explicit jar later.

    Reproduced drift: bootstrap wrote the store, the operator then switched to
    FB_COOKIE_FILE, and invalidate() still discarded the store and cleared the
    explicit session — the next call would have bootstrapped the browser
    identity instead.
    """
    settings = _settings(tmp_path)
    SessionManager(settings, lambda: {"c_user": "111", "xs": "boot"}).require_auth()

    store = AccountPaths.for_account("default", tmp_path)
    assert store.cookies_file.is_file()

    explicit = tmp_path / "explicit.txt"
    explicit.write_text(NETSCAPE.replace("c_user\t123", "c_user\t222"), encoding="utf-8")
    manager = SessionManager(_settings(tmp_path, cookie_file=str(explicit)))
    assert manager.source == "cookie-file"
    assert manager.state.user_id == "222"

    manager.invalidate("HTTP 403")

    assert store.cookies_file.is_file(), "another session's store jar was deleted"
    assert explicit.is_file(), "the user's explicit jar was deleted"


def test_source_reports_where_the_cookies_came_from(tmp_path: Path) -> None:
    store = AccountPaths.for_account("default", tmp_path).ensure()
    store.cookies_file.write_text(NETSCAPE, encoding="utf-8")
    assert SessionManager(_settings(tmp_path)).source == "account-store"

    boot = SessionManager(_settings(tmp_path / "other"), lambda: {"c_user": "9", "xs": "t"})
    boot.require_auth()
    assert boot.source == "bootstrap"

    inline = SessionManager(_settings(tmp_path / "third", cookie="c_user=1; xs=2"))
    assert inline.source == "cookie"

    anonymous = SessionManager(_settings(tmp_path / "fourth"))
    assert anonymous.source == "none"


def test_a_persisted_jar_stays_refreshable_after_a_restart(tmp_path: Path) -> None:
    """The store remembers it came from the browser, so a restart can refresh it."""
    bootstrap = SessionManager(_settings(tmp_path), lambda: {"c_user": "7", "xs": "tok"})
    bootstrap.require_auth()
    assert bootstrap.source == "bootstrap"

    # New process: loads the same file, but the provenance is not lost.
    reopened = SessionManager(_settings(tmp_path), lambda: {"c_user": "8", "xs": "fresh"})
    assert reopened.source == "bootstrap", "the bootstrap origin was forgotten"

    reopened.invalidate("HTTP 403")
    assert not AccountPaths.for_account("default", tmp_path).cookies_file.exists()

    # … and it re-bootstraps instead of replaying the dead cookie.
    assert reopened.require_auth().user_id == "8"
    assert reopened.source == "bootstrap"


def test_invalidation_discards_only_a_bootstrapped_jar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A jar the user imported must survive a rejection; ours may be re-created."""
    monkeypatch.setenv("FB_HOME", str(tmp_path))

    imported = AccountPaths.for_account("imported", tmp_path).ensure()
    imported.cookies_file.write_text(NETSCAPE, encoding="utf-8")
    imported.merge_meta(source="import")
    session = SessionManager(Settings(account="imported"))
    assert session.source == "account-store"
    session.invalidate("HTTP 403 on /x")
    assert imported.cookies_file.is_file(), "a user-supplied jar was deleted"

    bootstrapped = AccountPaths.for_account("bootstrapped", tmp_path).ensure()
    bootstrapped.cookies_file.write_text(NETSCAPE, encoding="utf-8")
    bootstrapped.merge_meta(source="cdp-bootstrap")
    session = SessionManager(Settings(account="bootstrapped"))
    assert session.source == "bootstrap"
    session.invalidate("HTTP 403 on /x")
    assert not bootstrapped.cookies_file.exists(), "our own cache should be discarded"
