#!/usr/bin/env python
"""Manual probe: exercise a service against the live site.

Usage::

    .venv/bin/python tools/probe.py profile <username>
    .venv/bin/python tools/probe.py search people "pest control"

This is a development/verification helper, not part of the runtime.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facebook_mcp.core.exceptions import FacebookError
from facebook_mcp.services.factory import get_services


def _dump(label: str, value: object) -> None:
    print(f"\n=== {label} ===")
    if hasattr(value, "model_dump"):
        print(json.dumps(value.model_dump(), indent=2, ensure_ascii=False, default=str))
    elif isinstance(value, list):
        print(
            json.dumps(
                [v.model_dump() if hasattr(v, "model_dump") else v for v in value],
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    else:
        print(value)


def main(argv: list[str]) -> int:
    services = get_services()
    try:
        if len(argv) < 2:
            print(__doc__)
            return 2

        command = argv[1]

        if command == "profile":
            if len(argv) < 3:
                print("usage: probe.py profile <username>", file=sys.stderr)
                return 2
            username = argv[2]
            _dump("latest post time", services.post.get_latest_post_time(username))
            _dump("recent posts", services.post.get_recent_posts(username, limit=5))

        elif command == "search":
            if len(argv) < 4:
                print("usage: probe.py search <people|pages> <query>")
                return 2
            kind, query = argv[2], argv[3]
            if kind == "people":
                _dump("people", services.search.search_people(query))
            elif kind == "pages":
                _dump("pages", services.search.search_pages(query))
            else:
                print(f"unknown search kind: {kind}")
                return 2

        else:
            print(f"unknown command: {command}")
            return 2

    except FacebookError as exc:
        print(f"\n[{exc.code}] {exc}", file=sys.stderr)
        return 1
    finally:
        services.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
