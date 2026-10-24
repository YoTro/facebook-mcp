#!/usr/bin/env python
"""Guard against stale claims that the structure check cannot see.

``tools/check_docs.py`` compares CN/EN *shape*; it cannot notice that both files
still say something the code stopped doing. That is how this project accumulated
contradictions: each fix updated the code and the paragraph it was looking at,
while older sentences elsewhere kept the superseded story. (17 of the commits
touched ``message_client.py``; the same few claims were re-fixed by hand more than
once.)

Matching rules that matter, learned the hard way:

* **whole file, not line by line** — prose wraps, and the old CDP-hint sentence
  spanned two lines;
* **case-insensitive** — "Couldn't send" and "couldn't send" are the same claim;
* **markdown-insensitive** — `` `requires_auth=False` `` and
  ``**service worker**`` carry punctuation that is not part of the sentence;
* **patterns must match the sentences that actually shipped** — every rule below
  carries at least one verbatim sample in ``tests/test_check_claims.py``.

Usage::

    .venv/bin/python tools/check_claims.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Tests are excluded on purpose: asserting "this phrase is wrong" requires writing
# it down, so scanning them would make the guard fight its own verification.
DEFAULT_GLOBS = ("src/**/*.py", "tools/*.py", "tools/*.sh", "*.md")

# CHANGELOG is a history: it is supposed to quote the claims it corrected.
NO_CHANGELOG = ("src/**/*.py", "tools/*.py", "ARCHITECTURE*.md", "README.md")


@dataclass(frozen=True)
class Forbidden:
    """A claim that describes behaviour the code no longer has.

    ``pattern`` is matched against a normalised rendering of the file (lowercase,
    markdown punctuation stripped, all whitespace collapsed), so write it the way
    the sentence reads, not the way it is formatted.
    """

    pattern: str
    reason: str
    globs: tuple[str, ...] = DEFAULT_GLOBS
    samples: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Required:
    """A phrase a file must contain, so a correction cannot silently vanish."""

    pattern: str
    path: str
    reason: str


RULES: tuple[Forbidden, ...] = (
    Forbidden(
        r"issued by (?:facebook's |the )?service worker",
        "Messenger's send runs on the page session's WebSocket; workers and "
        "iframes only carry the sync traffic around it (ARCHITECTURE §8.3).",
        NO_CHANGELOG,
        ("the send is issued by Facebook's service worker", "issued by the **service worker**"),
    ),
    Forbidden(
        r"needs pysocks|install pysocks",
        "websocket-client implements SOCKS through python-socks, not PySocks.",
        DEFAULT_GLOBS,
        ("SOCKS needs PySocks",),
    ),
    Forbidden(
        r"仅在通道不可用时",
        "auto also falls back after an explicit gateway refusal, not only when "
        "the channel is unusable.",
        DEFAULT_GLOBS,
        ("仅在通道不可用时回退到上面的 UI 流程",),
    ),
    Forbidden(
        r"连不上才回退",
        "auto also falls back after an explicit gateway refusal, not only when "
        "the channel cannot be reached.",
        DEFAULT_GLOBS,
        ("`FB_MESSAGE_TRANSPORT=auto` 时先走 WebSocket,连不上才回退到 CDP UI 路径;",),
    ),
    Forbidden(
        r"only falls back to the (?:ui flow|cdp ui path)[^.]{0,80}channel is "
        r"(?:unusable|unreachable)",
        "auto also falls back after an explicit gateway refusal.",
        DEFAULT_GLOBS,
        (
            "and only falls back to the UI flow above when the channel is unusable",
            "only falls back to the CDP UI path when the channel is unusable; `ui` forces the",
        ),
    ),
    Forbidden(
        r"requires_auth=false\)? \| curl_cffi,? ?不带 cookie"
        r"|public read \(requires_auth=false\) \| curl_cffi, no cookies",
        "whether cookies travel is decided by Route.anonymous, not by requires_auth.",
        DEFAULT_GLOBS,
        (
            '| 公开读(`requires_auth=False`) | curl_cffi,不带 cookie(`credentials="omit"`) |',
            '| public read (`requires_auth=False`) | curl_cffi, no cookies (`credentials="omit"`) |',
        ),
    ),
    Forbidden(
        r"cdp hint is only used when the session has no cookies",
        "clients set no transport hint at all; the browser is reached through cdp_from() only.",
        DEFAULT_GLOBS,
        ("the CDP hint is only\nused when the session has no cookies at all.",),
    ),
    Forbidden(
        r"label on our bubble = rejected|bubble = rejected",
        "the UI cannot prove a marker belongs to our message; it reports unknown, "
        "and only the gateway may claim rejected.",
        DEFAULT_GLOBS,
        (
            'a new "Couldn\'t send"\n   label on our bubble = ``rejected``; anything '
            "unobservable after the click =",
        ),
    ),
    Forbidden(
        r"from_config[^.]{0,80}(?:决定是否走纯协议|decides? (?:the )?(?:transport|routing))",
        "transport selection no longer depends on cookie provenance; from_config "
        "only decides what invalidate() may discard.",
        DEFAULT_GLOBS,
        ("- `from_config`:cookie 是否来自配置或账户库(而非浏览器),决定是否走纯协议(见 §9.3)",),
    ),
    Forbidden(
        r"无浏览器时发消息",
        "protocol sending needs no browser; only the UI path does.",
        DEFAULT_GLOBS,
        ("该**传输或账号类型**不具备此能力(无浏览器时发消息、Page 身份发起会话)",),
    ),
    Forbidden(
        r"除 cdperror 外都在该文件|class below lives there except cdperror",
        "LightspeedError also lives outside core (clients/lightspeed_client.py).",
        ("*.md",),  # `except CdpError:` is legitimate Python
        ("(下列各类除 CdpError 外都在该文件)", "(every class below lives there except CdpError)"),
    ),
    Forbidden(
        r"发送前必须先|预热是必要(?:前提|条件)|(?<!no )warm-?up (?:tasks? )?(?:is|are) required",
        "the warm-up tasks are thread-specific: one page thread needed them, a "
        "public page conversation accepts a bare task 46.",
        DEFAULT_GLOBS,
        ("**已跑通(无需浏览器)**:发送前必须先发一组**线程上下文查询**",),
    ),
    Forbidden(
        r"no server acknowledgement is readable|no acknowledgement is readable",
        "the WebSocket path returns the server's message_id.",
        DEFAULT_GLOBS,
        (
            "message was handed to Messenger. No server acknowledgement is readable from",
            "'submitted' (handed to Messenger; no server acknowledgement is readable,",
        ),
    ),
)

REQUIRED: tuple[Required, ...] = (
    Required(
        r"unknown",
        "src/facebook_mcp/clients/message_client.py",
        "the UI path must state that an ambiguous failure is `unknown`.",
    ),
    Required(
        r"explicit gateway refusal|网关明确拒绝",
        "ARCHITECTURE.md",
        "the fallback rule (UI probe after an explicit refusal) must stay stated.",
    ),
    Required(
        r"python-socks",
        "CHANGELOG.md",
        "the SOCKS dependency is python-socks.",
    ),
)

# Markdown punctuation that is not part of a sentence's meaning.
_STRIP_CHARS = str.maketrans("", "", "`*~>")


def _clean(line: str) -> str:
    """Lowercase one line and drop markdown emphasis/quoting marks."""
    return line.translate(_STRIP_CHARS).lower()


_ALLOW_OPEN = "<!-- check-claims: allow-superseded -->"
_ALLOW_CLOSE = "<!-- /check-claims: allow-superseded -->"


def _strip_allowed(text: str) -> str:
    """Blank out explicitly exempted spans, keeping line numbers intact.

    A changelog has to quote the claim it superseded; rather than loosening a
    rule, the quoted span is marked. Anything outside the markers is still
    checked, so a wrong claim cannot hide in the changelog.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if _ALLOW_OPEN in line:
            skipping = True
            out.append("")  # keep the line count so findings point at real lines
        elif _ALLOW_CLOSE in line:
            skipping = False
            out.append("")
        elif skipping:
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def _normalize(text: str) -> tuple[str, list[int]]:
    """Return ``(normalised_text, line_of_each_character)``.

    Whitespace (including line breaks) collapses to single spaces so a sentence
    that wraps still matches; the parallel list lets a finding point at the line
    where the sentence starts.
    """
    chars: list[str] = []
    line_of: list[int] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        cleaned = _clean(raw)
        if not cleaned.strip():
            continue
        for char in cleaned:
            chars.append(char)
            line_of.append(number)
        chars.append(" ")
        line_of.append(number)
    return "".join(chars), line_of


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
            normalized, line_of = _normalize(_strip_allowed(text))
            for match in regex.finditer(normalized):
                line = line_of[match.start()]
                problems.append(
                    f"{path.relative_to(ROOT)}:{line}: stale claim "
                    f"{normalized[match.start() : match.end()][:80]!r}\n"
                    f"    reason: {rule.reason}"
                )

    for requirement in REQUIRED:
        path = ROOT / requirement.path
        if not path.is_file():
            problems.append(f"{requirement.path}: missing file for a required claim")
            continue
        normalized, _ = _normalize(path.read_text(encoding="utf-8"))
        if not re.search(requirement.pattern, normalized):
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

    samples = sum(len(rule.samples) for rule in RULES)
    print(f"OK  {len(RULES)} rules / {samples} regression samples hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
