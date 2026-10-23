"""The stale-claim guard must fail on the claims it exists for.

Its whole point is that a correction survives: a grep done once and forgotten is
how the same statement got re-fixed three times.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import check_claims


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(check_claims, "ROOT", tmp_path)
    return tmp_path


def _rule(pattern: str) -> check_claims.Forbidden:
    return next(rule for rule in check_claims.RULES if rule.pattern == pattern)


def test_forbidden_claim_is_detected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(r"needs PySocks|install PySocks")
    (repo / "src" / "a.py").write_text("# SOCKS needs PySocks here\n")
    monkeypatch.setattr(check_claims, "RULES", (rule,))
    monkeypatch.setattr(check_claims, "REQUIRED", ())

    problems = check_claims.check()
    assert problems and "needs PySocks" in problems[0]
    assert "python-socks" in problems[0], "the reason must travel with the finding"


def test_clean_repo_passes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "src" / "a.py").write_text("# SOCKS uses python-socks\n")
    monkeypatch.setattr(check_claims, "RULES", (_rule(r"needs PySocks|install PySocks"),))
    monkeypatch.setattr(check_claims, "REQUIRED", ())
    assert check_claims.check() == []


def test_changelog_may_quote_superseded_claims(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """History is allowed to name the wrong claim it is correcting."""
    rule = _rule(r"issued by a?\s*service worker|send (?:is )?issued by (?:the )?service worker")
    (repo / "CHANGELOG.md").write_text(
        "Earlier notes said the send is issued by a service worker.\n"
    )
    monkeypatch.setattr(check_claims, "RULES", (rule,))
    monkeypatch.setattr(check_claims, "REQUIRED", ())
    assert check_claims.check() == []


def test_missing_required_claim_is_reported(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "CHANGELOG.md").write_text("- SOCKS needs PySocks\n")
    monkeypatch.setattr(check_claims, "RULES", ())
    monkeypatch.setattr(
        check_claims,
        "REQUIRED",
        (check_claims.Required(r"python-socks", "CHANGELOG.md", "tests the rule"),),
    )
    problems = check_claims.check()
    assert problems and "missing required claim" in problems[0]


def test_repository_is_clean() -> None:
    """The real tree must satisfy every rule."""
    assert check_claims.check() == []
