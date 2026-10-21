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

Captured ``variables`` blobs contain session-derived identifiers, so they are
written to the gitignored ``--out`` path (``.cache/routes.json``). With
``--save-templates`` the capture is additionally made live: the search
``variables`` template is promoted into ``src/facebook_mcp/routes/templates/``
(runtime reads it) and fresh ``doc_id`` values are recorded in the account
store, so refreshing them after a Facebook build needs no source change.

Usage::

    .venv/bin/python tools/sniff.py                 # capture for 60s
    .venv/bin/python tools/sniff.py --seconds 120
    .venv/bin/python tools/sniff.py --url-contains send_message
    .venv/bin/python tools/sniff.py --save-templates
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facebook_mcp.core.config import Settings, get_settings
from facebook_mcp.routes.graphql import ALL as GRAPHQL_ROUTES
from facebook_mcp.transport.cdp import CdpTransport

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_TEMPLATES = REPO_ROOT / "src/facebook_mcp/routes/templates"

# Only captured queries with a consumer in ``routes/templates/__init__.py`` are
# promoted into the package. Everything else is already recorded in ``--out``;
# copying it into ``routes/templates/`` would ship dead captures — including
# session-derived ids — inside the wheel.
PROMOTED_TEMPLATES = frozenset({"SearchCometResultsPaginatedResultsQuery"})


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
        help=(
            "make this capture live: promote the search variables template and "
            "record fresh doc_ids in the account store (no source change)"
        ),
    )
    args = parser.parse_args(argv)

    if args.save_templates and not (REPO_ROOT / ".git").exists():
        print(
            "--save-templates only works from a source checkout: "
            f"{PACKAGE_TEMPLATES} is inside an installed package here.\n"
            "Capture with --out instead and promote the template by hand.",
            file=sys.stderr,
        )
        return 2

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
            saved = _save_template(str(friendly or "unknown"), entry["variables"])
            if saved is not None:
                print(f"    template  -> {_short(saved)}")

        print(f"+ {friendly or '?'}")
        print(f"    doc_id    : {doc_id}")
        print(f"    variables : {json.dumps(entry['variables'])[:140]}\n")

    print(f"\ncaptured {len(captured)} unique route(s)")
    if captured:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        out.write_text(json.dumps(list(captured.values()), indent=2), encoding="utf-8")
        # Captured ``variables`` carry session-derived ids; keep them private.
        out.chmod(0o600)
        print(f"written to {out}\n")
        _print_route_table(captured)
        if args.save_templates:
            meta = _record_doc_ids(settings, captured)
            if meta is not None:
                print(f"doc_ids   -> {_short(meta)}  (no source change needed)\n")

    transport.close()
    return 0


def _try_json(value: str | None) -> object:
    if not value:
        return None
    try:
        return json.loads(unquote(value))
    except ValueError:
        return value[:300]


def _save_template(friendly_name: str, variables: object) -> Path | None:
    """Promote a captured query into a template the runtime actually reads.

    Returns the written path, or ``None`` when the capture is not promoted.
    """
    if friendly_name not in PROMOTED_TEMPLATES:
        return None

    target = PACKAGE_TEMPLATES / "search_results.json"
    payload = json.loads(json.dumps(variables))
    args = payload.setdefault("args", {})
    args["text"] = "{query}"
    args.setdefault("experience", {})["type"] = "{experience_type}"
    args.setdefault("context", {})["bsid"] = "{bsid}"
    payload["cursor"] = '{"page_number":{page_number},"flow_cursors_serialized":{}}'
    _write_atomic(target, json.dumps(payload, indent=2, sort_keys=True))
    return target


def _record_doc_ids(settings: Settings, captured: dict[str, dict[str, object]]) -> Path | None:
    """Persist freshly captured ``doc_id`` values into the account store.

    ``routes/graphql.py`` only carries a bootstrap default; the live value is
    read from ``account.json`` at request time (``BaseClient._doc_id``), because
    ``doc_id`` rotates with every frontend build. Returns the metadata file it
    touched, or ``None`` when nothing captured maps onto a known route.
    """
    ops = {r.friendly_name: r.op for r in GRAPHQL_ROUTES if r.friendly_name}
    fresh: dict[str, str] = {}
    for entry in captured.values():
        op = ops.get(str(entry.get("friendly_name") or ""))
        doc_id = entry.get("doc_id")
        if op and doc_id:
            fresh[op] = str(doc_id)
    if not fresh:
        return None

    paths = settings.account_paths()
    existing = paths.load_meta().get("doc_ids")
    merged = {**(existing if isinstance(existing, dict) else {}), **fresh}
    paths.merge_meta(doc_ids=merged)
    return paths.meta_file


def _short(path: Path) -> str:
    """Render ``path`` relative to ``~`` or the repo root when possible."""
    for base, prefix in ((Path.home(), "~/"), (REPO_ROOT, "")):
        try:
            return prefix + str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def _write_atomic(target: Path, text: str) -> None:
    """Replace ``target`` atomically so a failed capture cannot truncate it.

    ``search_results.json`` is loaded on every search; a half-written file
    would surface as ``TemplateError`` until the next successful capture.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


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
