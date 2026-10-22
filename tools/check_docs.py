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


def _parse(path: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return ``(outline, bodies)`` for a document.

    ``outline`` lists **every** ``##``/``###`` heading in order, as
    ``("n", "3.1")`` for numbered headings and ``("u", "<ordinal>")`` for
    unnumbered ones. Comparing it catches a section that exists in both files but
    sits under a different parent — and a whole unnumbered section that vanished
    on one side, which a numbers-only view cannot see.

    ``bodies`` maps each *numbered* section to its body, including the text of
    any unnumbered headings nested under it (a code block moved into an
    unnumbered subsection still changes the parent's shape).
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    outline: list[tuple[str, str]] = []
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    unnumbered = 0

    for line in lines:
        heading = re.match(r"^(#{2,3})\s+(.*)$", line)
        if not heading:
            if current is not None:
                bodies[current].append(line)
            continue

        numbered = re.match(r"(?:#{2,3})\s+([0-9]+(?:\.[0-9]+)?)[.\s]", line)
        if numbered:
            current = numbered.group(1)
            outline.append(("n", current))
            bodies.setdefault(current, [])
        else:
            outline.append(("u", str(unnumbered)))
            unnumbered += 1

    return outline, {key: "".join(value) for key, value in bodies.items()}


def _sections(path: Path) -> dict[str, str]:
    """Backwards-compatible accessor: numbered section bodies only."""
    return _parse(path)[1]


def _shape(body: str) -> tuple[int, int, int, int]:
    bullets = len(re.findall(r"^\s*[-*] ", body, flags=re.M))
    rows = len(re.findall(r"^\s*\|.*\|\s*$", body, flags=re.M))
    blocks = re.findall(r"```[^\n]*\n(.*?)```", body, flags=re.S)
    code_lines = sum(len(block.rstrip("\n").splitlines()) for block in blocks)
    return bullets, rows, len(blocks), code_lines


def main() -> int:
    cn_outline, cn = _parse(DOCS["cn"])
    en_outline, en = _parse(DOCS["en"])
    problems: list[str] = []

    if list(cn) != list(en):
        missing = [key for key in cn if key not in en]
        extra = [key for key in en if key not in cn]
        if missing:
            problems.append(f"sections only in CN: {missing}")
        if extra:
            problems.append(f"sections only in EN: {extra}")

    if cn_outline != en_outline:
        # Report the first divergence: a moved unnumbered section shows up as a
        # different order of ("n", ...) / ("u", ...) markers.
        for index, (left, right) in enumerate(zip(cn_outline, en_outline, strict=False)):
            if left != right:
                problems.append(
                    f"outline diverges at heading #{index + 1}: "
                    f"CN={left} EN={right} (same sections, different parent)"
                )
                break
        else:
            longer = "CN" if len(cn_outline) > len(en_outline) else "EN"
            problems.append(
                f"outline length differs: CN={len(cn_outline)} EN={len(en_outline)} "
                f"({longer} has extra unnumbered headings)"
            )

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

    print(
        f"OK  {len(cn)} numbered sections, {len(cn_outline)} headings in sync "
        "(outline / bullets / tables / code blocks)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
