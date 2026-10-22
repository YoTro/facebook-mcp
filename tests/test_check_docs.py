"""The document-sync checker must actually catch one-sided edits.

The drift it exists for is invisible in review: an edit batch applied to the
Chinese file only, or a fenced block that loses a line on one side. A checker
that stays green on that is worse than none.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import check_docs


def _pair(tmp_path: Path, cn: str, en: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cn_path = tmp_path / "a.md"
    en_path = tmp_path / "b.md"
    cn_path.write_text(cn, encoding="utf-8")
    en_path.write_text(en, encoding="utf-8")
    monkeypatch.setattr(check_docs, "DOCS", {"cn": cn_path, "en": en_path})


BODY = """## 1. Intro

- one
- two

| a | b |
|---|---|
| 1 | 2 |

```
alpha
beta
```
"""


def test_in_sync_documents_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pair(tmp_path, BODY, BODY, monkeypatch)
    assert check_docs.main() == 0


def test_missing_section_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pair(tmp_path, BODY + "## 2. Extra\n", BODY, monkeypatch)
    assert check_docs.main() == 1


def test_lost_code_line_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly the §6.3 drift: one fenced line missing on the English side."""
    _pair(tmp_path, BODY, BODY.replace("beta\n", ""), monkeypatch)
    assert check_docs.main() == 1


def test_lost_bullet_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pair(tmp_path, BODY, BODY.replace("- two\n", ""), monkeypatch)
    assert check_docs.main() == 1


def test_missing_table_row_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pair(tmp_path, BODY, BODY.replace("| 1 | 2 |\n", ""), monkeypatch)
    assert check_docs.main() == 1


UNNUMBERED = """## 1. Intro

- one

### A note that has no number

```
alpha
```

## 2. Next

- two
"""


def test_moved_unnumbered_section_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The drift this catches: same sections, different parent section."""
    moved = """## 1. Intro

- one

## 2. Next

### A note that has no number

```
alpha
```

- two
"""
    _pair(tmp_path, UNNUMBERED, moved, monkeypatch)
    assert check_docs.main() == 1


def test_missing_unnumbered_section_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    without = """## 1. Intro

- one

## 2. Next

- two
"""
    _pair(tmp_path, UNNUMBERED, without, monkeypatch)
    assert check_docs.main() == 1


def test_unnumbered_bodies_count_towards_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code line lost inside an unnumbered subsection must still be noticed."""
    lost = UNNUMBERED.replace("alpha\n", "")
    _pair(tmp_path, UNNUMBERED, lost, monkeypatch)
    assert check_docs.main() == 1
