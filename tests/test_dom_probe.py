"""Execute the real rejection probe instead of inspecting its source text.

`FakeCdp` cannot run JavaScript, so the earlier tests only asserted that the
generated script *looked* right. These cases run the script in Node against a
minimal DOM, which is what actually decides whether a bubble counts as failed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from facebook_mcp.clients.message_client import _failed_bubbles

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to run the DOM probe")


class _CaptureCdp:
    """BrowserTransport double that records the probe instead of running it."""

    def __init__(self) -> None:
        self.expression = ""
        self.result: Any = 0

    @property
    def name(self) -> str:
        return "capture"

    def evaluate(self, expression: str, **kwargs: Any) -> Any:
        self.expression = expression
        return self.result

    def navigate(self, url: str, *, timeout: float | None = None) -> None:  # pragma: no cover
        pass

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    def request(self, req: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("not used")

    def close(self) -> None:
        pass


# --- a tiny DOM, just enough for the probe ------------------------------


def _run(probe: str, flat: list[dict[str, Any]]) -> int:
    """Execute the real probe script with ``flat`` behind querySelectorAll('*')."""
    script = f"""
const FLAT = ({json.dumps(flat)}).map((props) => ({{
  childElementCount: props.children || 0,
  textContent: props.text,
  innerText: props.innerText !== undefined ? props.innerText : props.text,
  closest: () => (props.article === undefined ? null : props.article),
}}));
const main = {{ querySelectorAll: () => FLAT }};
globalThis.document = {{
  querySelector: (sel) => (sel === '[role="main"]' ? main : null),
  querySelectorAll: () => [],
}};
const out = ({probe.strip()});
process.stdout.write(String(out));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip() or 0)


def _probe(needle: str | None) -> str:
    cdp = _CaptureCdp()
    _failed_bubbles(cdp, needle)  # type: ignore[arg-type]
    return cdp.expression


@pytest.mark.parametrize(
    ("label", "flat", "needle", "expected"),
    [
        (
            "leaf status label inside our article",
            [
                {"text": "hello", "children": 0, "article": {"innerText": "hello"}},
                {"text": "Couldn't send", "children": 0, "article": {"innerText": "hello"}},
            ],
            "hello",
            1,
        ),
        (
            "message body that *is* the marker, other bubble",
            [
                {"text": "Couldn't send", "children": 0, "article": {"innerText": "Couldn't send"}},
            ],
            "hello",
            0,
        ),
        (
            "multi-line article matches a collapsed needle",
            [
                {
                    "text": "Hello\nExample User",
                    "children": 0,
                    "article": {"innerText": "Hello\nExample User"},
                },
                {
                    "text": "Couldn't send",
                    "children": 0,
                    "article": {"innerText": "Hello\nExample User"},
                },
            ],
            "Hello Example User",
            1,
        ),
        (
            "marker inside a longer text is not a status label",
            [
                {
                    "text": "fixed the Couldn't send issue",
                    "children": 0,
                    "article": {"innerText": "fixed the Couldn't send issue"},
                },
            ],
            "fixed the Couldn",
            0,
        ),
        (
            "container nodes with children are skipped",
            [
                {"text": "Couldn't send", "children": 2, "article": {"innerText": "hello"}},
            ],
            "hello",
            0,
        ),
    ],
)
def test_probe_counts_only_status_labels(
    label: str, flat: list[dict[str, Any]], needle: str, expected: int
) -> None:
    assert _run(_probe(needle), flat) == expected, label


def test_probe_without_a_needle_counts_every_status_label() -> None:
    flat = [
        {"text": "Couldn't send", "children": 0, "article": {"innerText": "a"}},
        {"text": "无法发送", "children": 0, "article": {"innerText": "b"}},
        {"text": "hello", "children": 0, "article": {"innerText": "hello"}},
    ]
    assert _run(_probe(None), flat) == 2


def test_own_message_that_is_exactly_the_marker_is_indistinguishable() -> None:
    """The case that forces the UI path to answer `unknown`, not `rejected`.

    A message whose body is exactly "Couldn't send" produces the same DOM shape as
    a status label, so the probe counts it. Reporting that as "rejected" would
    tell the caller a retry is safe — hence the UI never claims rejection.
    """
    flat = [
        {
            "text": "Couldn't send",
            "children": 0,
            "article": {"innerText": "Couldn't send"},
        }
    ]
    assert _run(_probe("Couldn't send"), flat) == 1
