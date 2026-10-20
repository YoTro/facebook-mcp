#!/usr/bin/env python
"""Packet capture / route discovery via Chrome DevTools.

Attaches to a running Chrome (``--remote-debugging-port=9222``), enables the
Network domain, then records GraphQL requests while *you* use Facebook
normally in the browser. For each captured request it extracts:

* the endpoint URL
* ``doc_id`` (GraphQL query id, from the form body)
* ``fb_api_req_friendly_name`` (human-readable query name)
* ``variables`` (to understand the required parameter shape)

and prints a ready-to-paste Python route table for ``routes/graphql.py``.

Usage::

    .venv/bin/python tools/sniff.py                 # capture for 60s
    .venv/bin/python tools/sniff.py --seconds 120
    .venv/bin/python tools/sniff.py --url-contains send_message
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facebook_mcp.core.config import get_settings
from facebook_mcp.transport.cdp import CdpTransport


def _form_fields(post_data: str | None) -> dict[str, str]:
    if not post_data:
        return {}
    return {k: v[0] for k, v in parse_qs(post_data, keep_blank_values=True).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture Facebook GraphQL routes via CDP")
    parser.add_argument("--seconds", type=float, default=60.0, help="capture duration")
    parser.add_argument("--url-contains", default="graphql", help="filter URL substring")
    parser.add_argument(
        "--navigate",
        default=None,
        help="navigate the page to this URL first (e.g. a search URL)",
    )
    parser.add_argument(
        "--out", default=".cache/routes.json", help="where to write captured routes"
    )
    parser.add_argument(
        "--save-templates",
        action="store_true",
        help="also write captured GraphQL variables into routes/templates/",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    transport = CdpTransport(settings)

    try:
        transport.send("Network.enable", {"maxPostDataSize": 65536})
    except Exception as exc:  # noqa: BLE001
        print(f"cannot attach to Chrome: {exc}", file=sys.stderr)
        print(
            "\nStart Chrome with remote debugging first:\n  bash tools/launch_chrome_debug.sh\n",
            file=sys.stderr,
        )
        return 1

    if args.navigate:
        print(f"navigating to {args.navigate} ...")
        transport.send("Page.enable")
        transport.send("Page.navigate", {"url": args.navigate})
        time.sleep(2)

    print(f"capturing for {args.seconds:.0f}s — now use Facebook in the browser:")
    print("  • open a profile timeline          (ProfileCometTimelineFeed*)")
    print("  • run a people/page search          (SearchCometResults*)")
    print("  • send a Messenger message          (SendMessageMutation)\n")

    captured: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + args.seconds
    ws = transport._connect()

    ws.settimeout(1.0)
    while time.monotonic() < deadline:
        try:
            raw = ws.recv()
        except Exception:  # noqa: BLE001 - timeout / socket closed
            continue
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("method") != "Network.requestWillBeSent":
            continue

        params = message.get("params") or {}
        request = params.get("request") or {}
        url = request.get("url") or ""
        if args.url_contains not in url:
            continue

        fields = _form_fields(request.get("postData"))
        doc_id = fields.get("doc_id") or fields.get("query_id")
        friendly = fields.get("fb_api_req_friendly_name") or request.get("X-FB-Friendly-Name")
        if not doc_id and not friendly:
            continue

        key = f"{friendly or '?'}:{doc_id or '?'}"
        if key in captured:
            continue

        entry = {
            "doc_id": doc_id,
            "friendly_name": friendly,
            "url": url,
            "method": request.get("method"),
            "variables": _try_json(fields.get("variables")),
        }
        captured[key] = entry

        if args.save_templates and entry["variables"] is not None:
            _save_template(str(friendly or "unknown"), entry["variables"])

        print(f"+ {friendly or '?'}")
        print(f"    doc_id    : {doc_id}")
        print(f"    variables : {json.dumps(entry['variables'])[:140]}\n")

    print(f"\ncaptured {len(captured)} unique route(s)")
    if captured:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(list(captured.values()), indent=2), encoding="utf-8")
        print(f"written to {out}\n")
        _print_route_table(captured)

    transport.close()
    return 0


def _try_json(value: str | None) -> object:
    if not value:
        return None
    try:
        return json.loads(unquote(value))
    except ValueError:
        return value[:300]


def _save_template(friendly_name: str, variables: object) -> None:
    """Persist captured variables where the clients expect them."""
    templates = Path(__file__).resolve().parents[1] / "src/facebook_mcp/routes/templates"
    templates.mkdir(parents=True, exist_ok=True)

    if friendly_name == "SearchCometResultsPaginatedResultsQuery":
        target = templates / "search_results.json"
        payload = json.loads(json.dumps(variables))
        payload["args"]["text"] = "{query}"
        payload["args"]["experience"]["type"] = "{experience_type}"
        payload["args"]["context"]["bsid"] = "{bsid}"
        payload["cursor"] = '{"page_number":{page_number},"flow_cursors_serialized":{}}'
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"    template  -> {target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target}"
        )
        return

    safe = _const_name(friendly_name).lower()
    target = templates / f"{safe}.json"
    target.write_text(json.dumps(variables, indent=2, sort_keys=True), encoding="utf-8")
    print(f"    template  -> {target.name}")


def _print_route_table(captured: dict[str, dict[str, object]]) -> None:
    print("Paste into src/facebook_mcp/routes/graphql.py:\n")
    for entry in captured.values():
        name = _const_name(str(entry["friendly_name"] or "UNKNOWN"))
        print(
            f"{name} = Route(\n"
            f'    op="{name.lower().replace("_", ".")}",\n'
            f'    method="POST",\n'
            f'    path="/api/graphql/",\n'
            f'    doc_id="{entry["doc_id"]}",\n'
            f'    friendly_name="{entry["friendly_name"]}",\n'
            f")\n"
        )


def _const_name(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch.isupper() and out and out[-1] != "_":
            out.append("_")
        out.append(ch if ch.isalnum() else "_")
    return "".join(out).upper().strip("_")


if __name__ == "__main__":
    raise SystemExit(main())
