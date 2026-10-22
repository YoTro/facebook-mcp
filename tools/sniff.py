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

from facebook_mcp.core.config import Settings
from facebook_mcp.routes.graphql import ALL as GRAPHQL_ROUTES
from facebook_mcp.services.factory import build_settings
from facebook_mcp.transport.cdp import CdpTransport

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_TEMPLATES = REPO_ROOT / "src/facebook_mcp/routes/templates"

# Only captured queries with a consumer in ``routes/templates/__init__.py`` are
# promoted into the package, and each one maps to the file that consumer reads.
# Everything else stays in ``--out``; copying it into ``routes/templates/`` would
# ship dead captures — including session-derived ids — inside the wheel.
PROMOTED_TEMPLATES: dict[str, str] = {
    "SearchCometResultsPaginatedResultsQuery": "search_results.json",
}


def _form_fields(post_data: str | None) -> dict[str, str]:
    if not post_data:
        return {}
    return {k: v[0] for k, v in parse_qs(post_data, keep_blank_values=True).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture Facebook GraphQL routes via CDP")
    parser.add_argument("--seconds", type=float, default=60.0, help="capture duration")
    parser.add_argument("--url-contains", default="graphql", help="filter URL substring")
    parser.add_argument(
        "--account",
        default=None,
        help="account whose cdp_url to attach to (default: FB_ACCOUNT)",
    )
    parser.add_argument("--home", default=None, help="credential root (default: FB_HOME)")
    parser.add_argument(
        "--websocket",
        action="store_true",
        help=(
            "also capture WebSocket URLs and frames. Messenger sends messages over "
            "wss://gateway.facebook.com/ws/lightspeed, which never shows up as an "
            "HTTP request"
        ),
    )
    parser.add_argument(
        "--frames-dir",
        default=".cache/ws-frames",
        help="where --websocket writes decoded binary frames",
    )
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

    # build_settings() applies the account's cdp_url from account.json; plain
    # get_settings() would attach to the global FB_CDP_URL and capture from the
    # wrong browser when several accounts run side by side.
    settings = build_settings(args.account, home=args.home)
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
    print("  • send a Messenger message          (needs --websocket: it travels over")
    print("                                       wss://gateway.facebook.com/ws/lightspeed)")
    print()

    captured: dict[str, dict[str, object]] = {}
    frames: list[dict[str, object]] = []
    deadline = time.monotonic() + args.seconds

    def on_entry(entry: dict[str, object]) -> None:
        key = f"{entry['friendly_name'] or '?'}:{entry['doc_id'] or '?'}"
        if key in captured:
            return
        captured[key] = entry

        if args.save_templates and entry["variables"] is not None:
            saved = _save_template(str(entry["friendly_name"] or "unknown"), entry["variables"])
            if saved is not None:
                print(f"    template  -> {_short(saved)}")

        print(f"+ {entry['friendly_name'] or '?'}")
        print(f"    doc_id    : {entry['doc_id']}")
        print(f"    variables : {json.dumps(entry['variables'])[:140]}\n")

    frames_dir = Path(args.frames_dir)
    on_websocket = (
        (lambda message: _record_websocket(message, frames, frames_dir)) if args.websocket else None
    )
    _capture(transport, deadline, args.url_contains, on_entry, on_websocket)

    if args.websocket:
        print(f"\ncaptured {len(frames)} websocket event(s)")

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
    filename = PROMOTED_TEMPLATES.get(friendly_name)
    if filename is None:
        return None

    target = PACKAGE_TEMPLATES / filename
    payload = json.loads(json.dumps(variables))
    args = payload.setdefault("args", {})
    args["text"] = "{query}"
    args.setdefault("experience", {})["type"] = "{experience_type}"
    args.setdefault("context", {})["bsid"] = "{bsid}"
    payload["cursor"] = '{"page_number":{page_number},"flow_cursors_serialized":{}}'
    _write_atomic(target, json.dumps(payload, indent=2, sort_keys=True))
    return target


def _record_websocket(
    message: dict[str, object], frames: list[dict[str, object]], directory: Path
) -> None:
    """Record one WebSocket event; binary frames are also written to disk.

    Lightspeed frames are binary with a 16-byte header (see
    ``facebook_mcp.transport.lightspeed``), so the payload is decoded from base64
    and saved rather than printed.
    """
    import base64

    params = message.get("params") or {}
    method = str(message.get("method"))
    session = message.get("sessionId") or "page"

    if method == "Network.webSocketCreated":
        entry = {"event": "open", "session": session, "url": params.get("url", "")}
        frames.append(entry)
        print(f"  ws open   [{session}] {str(entry['url'])[:96]}")
        return

    response = params.get("response") or {}
    payload = response.get("payloadData") or ""
    opcode = response.get("opcode")
    direction = "out" if method.endswith("Sent") else "in"
    raw = b""
    if opcode == 2:
        try:
            raw = base64.b64decode(payload)
        except Exception:  # noqa: BLE001 - malformed frame
            raw = b""
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{direction}-{len(raw)}.bin"
        (directory / name).write_bytes(raw)
        detail = raw[:64].hex(" ")
        text = raw[16:].decode("utf-8", "replace") if len(raw) > 16 else ""
        frames.append(
            {
                "event": direction,
                "session": session,
                "bytes": len(raw),
                "file": str(directory / name),
                "body": text[:400],
            }
        )
        if text.startswith("{"):
            print(f"  ws {direction:<3} [{session}] {len(raw)}B {text[:110]}")
        else:
            print(f"  ws {direction:<3} [{session}] {len(raw)}B {detail}")
    else:
        frames.append({"event": direction, "session": session, "text": payload[:400]})
        print(f"  ws {direction:<3} [{session}] text {payload[:110]}")


def _entry_from_event(message: dict[str, object], url_contains: str) -> dict[str, object] | None:
    """Turn a ``Network.requestWillBeSent`` event into a capture entry."""
    if message.get("method") != "Network.requestWillBeSent":
        return None

    params = message.get("params") or {}
    request = params.get("request") or {}
    url = request.get("url") or ""
    if url_contains not in url:
        return None

    fields = _form_fields(request.get("postData"))
    doc_id = fields.get("doc_id") or fields.get("query_id")
    friendly = fields.get("fb_api_req_friendly_name") or request.get("X-FB-Friendly-Name")
    if not doc_id and not friendly:
        return None

    return {
        "doc_id": doc_id,
        "friendly_name": friendly,
        "url": url,
        "method": request.get("method"),
        "variables": _try_json(fields.get("variables")),
    }


def _capture(
    transport: object,
    deadline: float,
    url_contains: str,
    on_entry,
    on_websocket=None,
) -> None:
    """Drain captured events until ``deadline``, reporting each new route.

    ``drain_events()`` returns the events buffered while a CDP command was
    waiting for its reply, then reads the socket. Reading the raw WebSocket
    instead would lose everything that arrived during ``Page.navigate()``.
    """
    websocket_events = (
        "Network.webSocketCreated",
        "Network.webSocketFrameSent",
        "Network.webSocketFrameReceived",
    )
    while time.monotonic() < deadline:
        for message in transport.drain_events(timeout=0.8, deadline=deadline):  # type: ignore[attr-defined]
            if on_websocket is not None and message.get("method") in websocket_events:
                on_websocket(message)
                continue
            entry = _entry_from_event(message, url_contains)
            if entry is not None:
                on_entry(entry)


def _record_doc_ids(settings: Settings, captured: dict[str, dict[str, object]]) -> Path | None:
    """Persist freshly captured ``doc_id`` values into the account store.

    ``routes/graphql.py`` only carries a bootstrap default; the live value is
    read from ``account.json`` when the server builds its services and cached
    per account (``BaseClient._doc_id``), so a running server must be restarted
    to pick up a refresh. ``doc_id`` rotates with every frontend build. Returns
    the metadata file it touched, or ``None`` when nothing captured maps onto a
    known route.
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
    print(
        "Bootstrap defaults for src/facebook_mcp/routes/graphql.py — paste these\n"
        "only to keep a fresh clone working. Routine rotations are absorbed by the\n"
        "account store (account.json -> doc_ids) and must NOT be committed.\n"
    )
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
