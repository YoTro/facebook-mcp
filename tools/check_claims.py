#!/usr/bin/env python
"""Guard against stale claims that the structure check cannot see.

``tools/check_docs.py`` compares CN/EN *shape*; it cannot notice that both files
still say something the code stopped doing. That is exactly how this project
accumulated contradictions: each fix updated the code and the paragraph it was
looking at, while older sentences elsewhere kept the superseded story. (17 of the
commits touched ``message_client.py``; the same few claims were re-fixed by hand
more than once.)

This tool holds a short, curated list of claims that must not appear (and a few
that must), each with the reason it is wrong. It is intentionally small: a rule
earns its place by pointing at a mistake that actually shipped.

Usage::

    .venv/bin/python tools/check_claims.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Tests are excluded on purpose: asserting "this phrase is wrong" requires writing
# it down, so scanning them would make the guard fight its own verification.
DEFAULT_GLOBS = ("src/**/*.py", "tools/*.py", "tools/*.sh", "*.md")


@dataclass(frozen=True)
class Forbidden:
    """A phrase that describes behaviour the code no longer has."""

    pattern: str
    reason: str
    globs: tuple[str, ...] = DEFAULT_GLOBS


@dataclass(frozen=True)
class Required:
    """A phrase a file must contain, so a correction cannot silently vanish."""

    pattern: str
    path: str
    reason: str


RULES: tuple[Forbidden, ...] = (
    Forbidden(
        r"issued by a?\s*service worker|send (?:is )?issued by (?:the )?service worker",
        "Messenger's send is a page-owned WebSocket; workers/iframes only carry "
        "sync traffic (ARCHITECTURE §8.3).",
        # CHANGELOG quotes superseded claims on purpose; it is a history, not a
        # description of current behaviour.
        ("src/**/*.py", "tools/*.py", "ARCHITECTURE*.md", "README.md"),
    ),
    Forbidden(
        r"needs PySocks|install PySocks",
        "websocket-client implements SOCKS through python-socks, not PySocks.",
    ),
    Forbidden(
        r"仅在通道不可用时|连不上才回退|only falls back when the channel is (?:unusable|unreachable)",
        "auto falls back to the UI on a recoverable channel error *and* on an "
        "explicit gateway rejection.",
    ),
    Forbidden(
        r"Requires a logged-in browser|Requires a\s+logged-in browser",
        "sending is protocol-first; a browser is only needed for the UI fallback "
        "and for cookie bootstrapping.",
    ),
    Forbidden(
        r"no server acknowledgement is readable|no acknowledgement is readable",
        "the WebSocket path returns the server's message_id.",
    ),
    Forbidden(
        r"new[^\n]{0,40}Couldn't send[^\n]{0,40}rejected",
        "the UI cannot prove a marker belongs to our message; it reports unknown.",
    ),
    Forbidden(
        r"from_config[^\n]{0,80}(?:决定选路|决定通道|drives? the (?:transport|routing))",
        "transport selection no longer depends on cookie provenance.",
    ),
    Forbidden(
        r"(?:authenticated|writes|写操作)[^\n]{0,40}(?:via|走)\s*CDP",
        "authenticated reads use curl_cffi once cookies exist; CDP is only for "
        "cookie bootstrapping and the UI path.",
    ),
    Forbidden(
        r"CDP hint is only used when the session has no cookies",
        "clients no longer set a transport hint at all.",
    ),
    Forbidden(
        r"requires_auth=False\)\s*\|\s*curl_cffi\s*,\s*不带 cookie",
        "whether cookies travel is decided by Route.anonymous, not by requires_auth.",
    ),
    Forbidden(
        r"除 CdpError 外都在该文件|every class below lives there except CdpError",
        "LightspeedError also lives outside core (clients/lightspeed_client.py).",
    ),
    Forbidden(
        r"无浏览器时发消息",
        "protocol sending needs no browser; only the UI path does.",
    ),
    Forbidden(
        r"void 0",
        "placeholder rule: keeps the tool exercised even if the list shrinks.",
        ("tests/nonexistent/*.py",),
    ),
)

REQUIRED: tuple[Required, ...] = (
    Required(
        r"unknown",
        "src/facebook_mcp/clients/message_client.py",
        "the UI path must document that an ambiguous failure is `unknown`.",
    ),
    Required(
        r"explicit gateway rejection|网关明确拒绝",
        "ARCHITECTURE.md",
        "the fallback rule (UI probe after an explicit refusal) must stay stated.",
    ),
    Required(
        r"python-socks",
        "CHANGELOG.md",
        "the SOCKS dependency is python-socks.",
    ),
)


def _files(globs: tuple[str, ...]) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in globs:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and ".venv" not in path.parts and path.name != Path(__file__).name:
                seen[path] = None
    return list(seen)


def check() -> list[str]:
    problems: list[str] = []

    for rule in RULES:
        regex = re.compile(rule.pattern)
        for path in _files(rule.globs):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    problems.append(
                        f"{path.relative_to(ROOT)}:{number}: stale claim "
                        f"{line.strip()[:70]!r}\n    reason: {rule.reason}"
                    )

    for requirement in REQUIRED:
        path = ROOT / requirement.path
        if not path.is_file():
            problems.append(f"{requirement.path}: missing file for a required claim")
            continue
        if not re.search(requirement.pattern, path.read_text(encoding="utf-8")):
            problems.append(
                f"{requirement.path}: missing required claim "
                f"{requirement.pattern!r}\n    reason: {requirement.reason}"
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check for stale documentation claims")
    parser.add_argument("--verbose", action="store_true")
    parser.parse_args(argv)

    problems = check()
    if problems:
        print("stale claims found:")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nFix the text to match the code, or drop the rule in "
            "tools/check_claims.py with a reason."
        )
        return 1

    print(f"OK  {len(RULES)} forbidden / {len(REQUIRED)} required claims hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
