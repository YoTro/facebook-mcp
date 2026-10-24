"""The stale-claim guard, exercised with the sentences that actually shipped.

Each rule carries verbatim samples taken from this repository's history. The
first version of this tool matched its own paraphrases only, so six real
regressions slipped through; these cases exist to keep it honest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import check_claims


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("src", "tools", "tests"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(check_claims, "ROOT", tmp_path)
    return tmp_path


def _scan(monkeypatch: pytest.MonkeyPatch, rules: tuple[check_claims.Forbidden, ...]) -> list[str]:
    monkeypatch.setattr(check_claims, "RULES", rules)
    monkeypatch.setattr(check_claims, "REQUIRED", ())
    return check_claims.check()


# --- every rule must catch its own samples ------------------------------


@pytest.mark.parametrize(
    ("rule", "sample"),
    [(rule, sample) for rule in check_claims.RULES for sample in rule.samples],
    ids=lambda value: (value.pattern if isinstance(value, check_claims.Forbidden) else value)[:40],
)
def test_rule_catches_its_historical_sample(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    rule: check_claims.Forbidden,
    sample: str,
) -> None:
    """Verbatim text from an old commit must be rejected by its rule."""
    target = repo / "ARCHITECTURE.md"
    target.write_text(f"# Notes\n\n{sample}\n", encoding="utf-8")

    monkeypatch.setattr(check_claims, "ROOT", repo)
    problems = _scan(monkeypatch, (rule,))

    assert problems, f"rule did not catch its own sample: {sample!r}"
    assert rule.reason in problems[0]


def test_every_rule_has_a_sample() -> None:
    """A rule without a historical sentence is a guess, not a guard."""
    missing = [rule.pattern for rule in check_claims.RULES if not rule.samples]
    assert not missing, f"rules without a regression sample: {missing}"


# --- normalisation the first version was missing -----------------------


def test_wrapped_sentence_is_matched(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prose wraps; a line-based scan misses the sentence."""
    rule = _rule(r"cdp hint is only used when the session has no cookies")
    (repo / "README.md").write_text(
        "Search never goes through the browser once cookies exist — the CDP hint is only\n"
        "used when the session has no cookies at all.\n",
        encoding="utf-8",
    )
    assert _scan(monkeypatch, (rule,))


def test_matching_is_case_insensitive(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(r"needs pysocks|install pysocks")
    (repo / "src" / "a.py").write_text("# SOCKS Needs PySocks\n")
    assert _scan(monkeypatch, (rule,))


def test_markdown_emphasis_and_backticks_are_ignored(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = _rule(r"issued by (?:facebook's |the )?service worker")
    (repo / "ARCHITECTURE.md").write_text("The send is issued by the **service worker**.\n")
    assert _scan(monkeypatch, (rule,))


def test_table_cell_with_backticks_is_matched(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(
        r"requires_auth=false\)? \| curl_cffi,? ?不带 cookie"
        r"|public read \(requires_auth=false\) \| curl_cffi, no cookies"
    )
    (repo / "ARCHITECTURE.md").write_text(
        '| 公开读(`requires_auth=False`) | curl_cffi,不带 cookie(`credentials="omit"`) |\n'
    )
    assert _scan(monkeypatch, (rule,))


# --- scope and exemptions ----------------------------------------------


def test_changelog_may_quote_a_superseded_send_claim(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = _rule(r"issued by (?:facebook's |the )?service worker")
    (repo / "CHANGELOG.md").write_text("Notes once said the send is issued by a service worker.\n")
    assert _scan(monkeypatch, (rule,)) == []


def test_allowed_span_is_skipped_but_the_rest_is_still_checked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = _rule(r"needs pysocks|install pysocks")
    (repo / "CHANGELOG.md").write_text(
        "<!-- check-claims: allow-superseded -->\n"
        "| old | SOCKS needs PySocks |\n"
        "<!-- /check-claims: allow-superseded -->\n"
        "SOCKS needs PySocks in current behaviour\n",
        encoding="utf-8",
    )
    problems = _scan(monkeypatch, (rule,))
    assert problems, "the claim outside the exemption was not checked"
    assert "CHANGELOG.md:4" in problems[0]


def test_line_number_points_at_the_start_of_the_sentence(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = _rule(r"连不上才回退")
    (repo / "ARCHITECTURE.md").write_text(
        "intro\n\n`FB_MESSAGE_TRANSPORT=auto` 时先走 WebSocket,\n连不上才回退到 CDP UI 路径;\n"
    )
    problems = _scan(monkeypatch, (rule,))
    # The match starts on the second half of the wrapped sentence.
    assert problems and "ARCHITECTURE.md:4" in problems[0]


# --- required claims and the live tree ---------------------------------


def test_missing_required_claim_is_reported(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "CHANGELOG.md").write_text("- SOCKS needs PySocks\n")
    monkeypatch.setattr(check_claims, "ROOT", repo)
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


def _rule(pattern: str) -> check_claims.Forbidden:
    return next(rule for rule in check_claims.RULES if rule.pattern == pattern)
