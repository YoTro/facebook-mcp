"""Generic HTML / embedded-JSON extraction helpers (L0 core layer).

These helpers are deliberately domain-agnostic: they know how to pull JSON
payloads out of an HTML document and walk them, but nothing about Facebook's
schema. Domain-specific lookups live in the client layer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup

JSON_SCRIPT_TYPE = "application/json"


def iter_json_scripts(html: str) -> Iterator[Any]:
    """Yield every parsed JSON payload embedded in `<script type="application/json">`.

    Facebook renders data into ``<script type="application/json" data-sjs>``
    blocks. Malformed blocks are skipped rather than raising.
    """
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", attrs={"type": JSON_SCRIPT_TYPE}):
        raw = script.string if script.string is not None else script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue


def walk(obj: Any) -> Iterator[Any]:
    """Depth-first walk over every node (dict / list / scalar) of ``obj``."""
    stack: list[Any] = [obj]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def find_first(root: Any, key: str) -> Any | None:
    """Return the value of the first dict node containing ``key``."""
    for node in walk(root):
        if isinstance(node, dict) and key in node:
            return node[key]
    return None


def find_all(root: Any, key: str) -> list[Any]:
    """Return the values of every dict node containing ``key``."""
    out: list[Any] = []
    for node in walk(root):
        if isinstance(node, dict) and key in node:
            out.append(node[key])
    return out


def dig(obj: Any, *path: str | int) -> Any | None:
    """Safely navigate nested dicts/lists, returning ``None`` on any miss."""
    current = obj
    for step in path:
        if isinstance(step, int):
            if not isinstance(current, (list, tuple)) or not (-len(current) <= step < len(current)):
                return None
            current = current[step]
        else:
            if not isinstance(current, dict) or step not in current:
                return None
            current = current[step]
    return current
