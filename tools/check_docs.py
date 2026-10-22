#!/usr/bin/env python
"""Check that ARCHITECTURE.md and ARCHITECTURE.en.md stay in sync.

CONTRIBUTING requires the two files to share section numbering and structure, and
they drift silently: prose differs by language, so a paragraph or a code block can
go missing on one side without any obvious diff. This compares structure only.

Rules:

* identical numbered sections, in the same order;
* the same number of bullets and table rows per section;
* the same number of fenced code blocks and code lines per section, allowing the
  known ``CODE_LINE_DELTA`` where the English tree is visually wider.

Usage::

    .venv/bin/python tools/check_docs.py
    make docs
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = {"cn": ROOT / "ARCHITECTURE.md", "en": ROOT / "ARCHITECTURE.en.md"}

# English-minus-Chinese code-line count, for blocks that legitimately differ in
# width (ASCII trees and aligned exception tables). Update only with a reason.
CODE_LINE_DELTA = {}


def _sections(path: Path) -> dict[str, str]:
    """Return ``{section number: body}`` for ``## x.`` / ``### x.y`` headings."""
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^(#{2,3} .*)$", text, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        match = re.match(r"#+\s+([0-9]+(?:\.[0-9]+)?)", parts[i])
        if match:
            out[match.group(1)] = parts[i + 1]
    return out


def _shape(body: str) -> tuple[int, int, int, int]:
    bullets = len(re.findall(r"^\s*[-*] ", body, flags=re.M))
    rows = len(re.findall(r"^\s*\|.*\|\s*$", body, flags=re.M))
    blocks = re.findall(r"```[^\n]*\n(.*?)```", body, flags=re.S)
    code_lines = sum(len(block.rstrip("\n").splitlines()) for block in blocks)
    return bullets, rows, len(blocks), code_lines


def main() -> int:
    cn, en = _sections(DOCS["cn"]), _sections(DOCS["en"])
    problems: list[str] = []

    if list(cn) != list(en):
        missing = [key for key in cn if key not in en]
        extra = [key for key in en if key not in cn]
        if missing:
            problems.append(f"sections only in CN: {missing}")
        if extra:
            problems.append(f"sections only in EN: {extra}")
        if not missing and not extra:
            problems.append("section order differs")

    for key in (k for k in cn if k in en):
        cn_shape, en_shape = _shape(cn[key]), _shape(en[key])
        names = ("bullets", "table rows", "code blocks", "code lines")
        for index, name in enumerate(names):
            expected = cn_shape[index]
            actual = en_shape[index]
            if index == 3:
                expected += CODE_LINE_DELTA.get(key, 0)
            if expected != actual:
                problems.append(
                    f"§{key}: {name} CN={cn_shape[index]} EN={actual} (expected EN={expected})"
                )

    if problems:
        print("docs out of sync between ARCHITECTURE.md and ARCHITECTURE.en.md:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nFix both files, or update CODE_LINE_DELTA in tools/check_docs.py.")
        return 1

    print(f"OK  {len(cn)} sections in sync (bullets / tables / code blocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
