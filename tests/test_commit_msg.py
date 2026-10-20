"""Tests for the commit-message convention (`tools/commit_msg.py`)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "commit_msg.py"

sys.path.insert(0, str(TOOL.parent))
import commit_msg  # noqa: E402

# --- accepted -----------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "feat(search): add pagination cursor",
        "fix(accounts): stop leaking cookies into the repo",
        "docs: document the incognito test matrix",
        "test(transport): cover retry after a 429",
        "refactor(core)!: drop the legacy helpers",
        "chore(deps): bump curl_cffi to 0.17",
        "ci: run tests on python 3.13",
        "feat(search): 增加分页游标",
        "fix: handle a nil response",
        "revert: feat(search): add pagination cursor",
        "Merge branch 'main' into feature/x",
        "fixup! feat(search): add pagination cursor",
        "squash! chore(deps): bump ruff",
    ],
)
def test_valid_messages(message: str) -> None:
    assert commit_msg.validate(message) == []


def test_valid_message_with_body_and_footer() -> None:
    message = (
        "feat(search): add pagination cursor\n"
        "\n"
        "The GraphQL response already returned page_info.end_cursor; this wires\n"
        "it into SearchClient.next_cursor so callers can page through results.\n"
        "\n"
        "Refs: #42\n"
    )
    assert commit_msg.validate(message) == []


def test_comment_lines_are_ignored() -> None:
    message = "feat(core): add account paths\n\n# this is a template comment\n# another one\n"
    assert commit_msg.validate(message) == []


# --- rejected -----------------------------------------------------------


def test_empty_message() -> None:
    assert commit_msg.validate("") == ["commit message is empty"]
    assert commit_msg.validate("\n\n# only comments\n") == ["commit message is empty"]


def test_missing_type() -> None:
    problems = commit_msg.validate("add search pagination")
    assert any("must match" in p for p in problems)


def test_unknown_type() -> None:
    problems = commit_msg.validate("feature(search): add pagination")
    assert any("unknown type 'feature'" in p for p in problems)


def test_unknown_scope() -> None:
    problems = commit_msg.validate("feat(nonsense): add pagination")
    assert any("unknown scope 'nonsense'" in p for p in problems)


def test_subject_ending_with_period() -> None:
    problems = commit_msg.validate("feat(search): add pagination.")
    assert any("must not end with a period" in p for p in problems)


def test_uppercase_ascii_subject() -> None:
    problems = commit_msg.validate("feat(search): Add pagination")
    assert any("lowercase" in p for p in problems)


def test_header_too_long() -> None:
    message = "feat(search): " + "x" * 80
    problems = commit_msg.validate(message)
    assert any("header is" in p for p in problems)


def test_subject_too_long() -> None:
    message = "feat(search): " + "x" * (commit_msg.MAX_SUBJECT + 1)
    problems = commit_msg.validate(message)
    assert any("subject is" in p for p in problems)


def test_missing_blank_line_before_body() -> None:
    message = "feat(search): add cursor\nbody starts immediately\n"
    problems = commit_msg.validate(message)
    assert any("blank line" in p for p in problems)


def test_body_line_too_long() -> None:
    message = "feat(search): add cursor\n\n" + "word " * 30 + "\n"
    problems = commit_msg.validate(message)
    assert any("max 100" in p for p in problems)


def test_long_unbroken_token_allowed() -> None:
    message = "feat(search): add cursor\n\nSee https://example.com/" + "a" * 120 + "\n"
    assert commit_msg.validate(message) == []


# --- CLI ----------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_cli_inline_ok() -> None:
    result = _run("-m", "feat(core): add account helper")
    assert result.returncode == 0


def test_cli_inline_failure_explains_format() -> None:
    result = _run("-m", "nope")
    assert result.returncode == 1
    assert "invalid commit message" in result.stderr
    assert "types :" in result.stderr


def test_cli_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text("feat(core): add account helper\n", encoding="utf-8")
    result = _run(str(path))
    assert result.returncode == 0


def test_cli_missing_file(tmp_path: Path) -> None:
    result = _run(str(tmp_path / "nope"))
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_cli_range_in_temp_repo(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "feat(core): first commit")

    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "test(core): second commit")

    result = subprocess.run(
        [sys.executable, str(TOOL), "--range", "HEAD~1..HEAD"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "1 commit(s) OK" in result.stdout

    (tmp_path / "c.txt").write_text("c", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "bad message")

    result = subprocess.run(
        [sys.executable, str(TOOL), "--range", "HEAD~1..HEAD"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "1/1 commit(s) failed" in result.stderr
