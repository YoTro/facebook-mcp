#!/usr/bin/env python
"""Validate commit messages against the project's Conventional Commits rules.

Usable three ways:

* as a ``commit-msg`` git hook (``make hooks`` installs it)

      tools/commit_msg.py .git/COMMIT_EDITMSG

* ad hoc

      tools/commit_msg.py --message "feat(search): paginate results"

* in CI, over a commit range

      tools/commit_msg.py --range origin/main..HEAD

Rules (see ``CONTRIBUTING.md``):

    <type>(<scope>): <subject>
    <BLANK LINE>
    <body>
    <BLANK LINE>
    <footer>

* ``type`` must be one of :data:`TYPES`
* ``scope`` is optional; if present it must be one of :data:`SCOPES`
* the header is at most 72 characters and the subject does not end with a period
* a subject starting with an ASCII letter must be lowercase (CJK is untouched)
* body lines are at most 100 characters (long unbroken tokens are allowed)
* ``Merge``, ``Revert``, ``fixup!`` and ``squash!`` commits are accepted as-is
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TYPES = (
    "feat",
    "fix",
    "docs",
    "test",
    "refactor",
    "perf",
    "build",
    "ci",
    "chore",
    "revert",
)

SCOPES = (
    # architectural layers
    "core",
    "transport",
    "session",
    "routes",
    "clients",
    "services",
    "server",
    # capabilities / components
    "accounts",
    "cdp",
    "http",
    "proxy",
    "profile",
    "post",
    "search",
    "message",
    # project plumbing
    "tools",
    "tests",
    "ci",
    "docs",
    "deps",
    "release",
)

MAX_HEADER = 72
MAX_BODY_LINE = 100
MAX_SUBJECT = 60

_HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9-]+)\))?(?P<bang>!)?: (?P<subject>.+)$"
)

_BYPASS_PREFIXES = ("Merge ", "Revert ", "fixup!", "squash!")


def _clean(message: str) -> list[str]:
    """Drop comment lines and trailing blank lines."""
    lines = [line.rstrip() for line in message.splitlines()]
    kept = [line for line in lines if not line.lstrip().startswith("#")]
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def validate(message: str) -> list[str]:
    """Return a list of human-readable problems (empty when valid)."""
    lines = _clean(message)
    if not lines:
        return ["commit message is empty"]

    header = lines[0]
    if header.startswith(_BYPASS_PREFIXES):
        return []

    match = _HEADER_RE.match(header)
    if not match:
        return [
            "header must match '<type>(<scope>): <subject>'",
            f"got: {header!r}",
            f"types: {', '.join(TYPES)}",
            f"scopes: {', '.join(SCOPES)}",
        ]

    problems: list[str] = []
    commit_type = match.group("type")
    scope = match.group("scope")
    subject = match.group("subject").strip()

    if commit_type not in TYPES:
        problems.append(f"unknown type {commit_type!r} (allowed: {', '.join(TYPES)})")
    if scope is not None and scope not in SCOPES:
        problems.append(f"unknown scope {scope!r} (allowed: {', '.join(SCOPES)})")

    if len(header) > MAX_HEADER:
        problems.append(f"header is {len(header)} chars, max {MAX_HEADER}")
    if len(subject) > MAX_SUBJECT:
        problems.append(f"subject is {len(subject)} chars, max {MAX_SUBJECT}")
    if subject.endswith("."):
        problems.append("subject must not end with a period")
    if subject and subject[0].isascii() and subject[0].isalpha() and not subject[0].islower():
        problems.append("subject must start lowercase (unless it begins with a name/acronym)")

    if len(lines) > 1:
        if lines[1].strip():
            problems.append("put a blank line between the header and the body")
        for index, line in enumerate(lines[2:], start=3):
            if len(line) <= MAX_BODY_LINE:
                continue
            # A single unbreakable token (URL, hash) cannot be wrapped; allow it.
            if any(len(word) > MAX_BODY_LINE for word in line.split()):
                continue
            problems.append(f"line {index} is {len(line)} chars, max {MAX_BODY_LINE}")

    return problems


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError as exc:  # pragma: no cover - git is assumed present
        raise SystemExit("error: git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SystemExit(f"error: git {' '.join(args)} failed: {detail}") from exc


def commits_in_range(rev_range: str) -> list[str]:
    """Return the full message of every commit in ``rev_range``."""
    raw = _git("log", "--format=%B%x00", rev_range)
    return [chunk.strip("\n") for chunk in raw.split("\x00") if chunk.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate commit messages")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("file", nargs="?", help="path to a commit message file (hook mode)")
    source.add_argument("--message", "-m", help="message to validate inline")
    source.add_argument(
        "--range", "-r", dest="rev_range", help="git revision range, e.g. main..HEAD"
    )
    args = parser.parse_args(argv)

    if args.rev_range:
        messages = commits_in_range(args.rev_range)
        if not messages:
            print(f"no commits in range {args.rev_range}")
            return 0
        failures = 0
        for message in messages:
            problems = validate(message)
            subject = message.splitlines()[0] if message else "(empty)"
            if problems:
                failures += 1
                # stdout keeps the per-commit report in order; the summary
                # goes to stderr so a hook/CI log still shows it prominently.
                print(f"✗ {subject}")
                for problem in problems:
                    print(f"    - {problem}")
            else:
                print(f"✓ {subject}")
        if failures:
            print(f"\n{failures}/{len(messages)} commit(s) failed", file=sys.stderr)
            return 1
        print(f"\n{len(messages)} commit(s) OK")
        return 0

    if args.message is not None:
        text = args.message
    else:
        path = Path(args.file or "")
        if not args.file or not path.is_file():
            print(f"error: commit message file not found: {args.file}", file=sys.stderr)
            return 1
        text = path.read_text(encoding="utf-8")

    problems = validate(text)
    if not problems:
        return 0

    print("invalid commit message:\n", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\nformat: <type>(<scope>): <subject>\n"
        f"  types : {', '.join(TYPES)}\n"
        f"  scopes: {', '.join(SCOPES)}\n"
        "  see CONTRIBUTING.md for examples\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
