#!/usr/bin/env python
"""Probe Messenger's Lightspeed WebSocket without a browser.

Why this exists: ``send_message`` already uses the protocol (see
``clients/lightspeed_client.py``); this tool is the manual counterpart, for
poking at the channel when something needs re-measuring. Recording the page
WebSocket is what made the protocol visible in the first place — messages travel
as JSON tasks on

    wss://gateway.facebook.com/ws/lightspeed

inside a 16-byte binary header that ``facebook_mcp.transport.lightspeed`` now
encodes byte-for-byte (see ``tests/test_lightspeed.py``). Auth is cookie-based
(``x-dgw-authtype=1:0``), so a plain Python WebSocket can reach the same stream.

Status: the protocol is implemented — ``clients/lightspeed_client.py`` is what
``send_message`` uses, and this tool exists to poke at the channel by hand
(``--dry-run`` prints the frame it would send). A refusal
(``markOptimisticMessageFailed``) is *ambiguous*: the recipient may have
messaging disabled, the thread type may need the UI path, or the thread may need
context (``--warmup``).

Usage::

    .venv/bin/python tools/lightspeed_probe.py --dry-run --thread 123 --text hi
    .venv/bin/python tools/lightspeed_probe.py --thread 123 --text hi
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facebook_mcp.core.accounts import AccountPaths
from facebook_mcp.routes.lightspeed import DEFAULT_VERSION_ID, gateway_url
from facebook_mcp.services.factory import build_settings
from facebook_mcp.session.extractor import from_cookie_file
from facebook_mcp.transport import lightspeed

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def load_cookies(account: str) -> dict[str, str]:
    settings = build_settings(account)
    paths = AccountPaths.for_account(settings.account, settings.home)
    if not paths.cookies_file.is_file():
        raise SystemExit(
            f"no cookies for account {settings.account!r}; run "
            f"tools/export_cookies.py --account {settings.account}"
        )
    return from_cookie_file(paths.cookies_file).cookies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the Lightspeed WebSocket")
    parser.add_argument("--account", default=None)
    parser.add_argument("--thread", required=True, help="Messenger thread id")
    parser.add_argument("--text", default="lightspeed probe")
    parser.add_argument(
        "--version-id",
        default="",
        help=(
            "client build id sent inside the envelope; defaults to the captured "
            "constant in routes/lightspeed.py (it was identical across sessions)"
        ),
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help=(
            "send the thread-context fetches first. Not required for a public page "
            "conversation; one page-owned thread only accepted the send with them"
        ),
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true", help="print the frame only")
    args = parser.parse_args(argv)

    cookies = load_cookies(args.account) if args.account else load_cookies(None)
    uid = cookies.get("c_user")
    if not uid:
        raise SystemExit("the cookie jar has no c_user")

    version_id = args.version_id or DEFAULT_VERSION_ID
    otid = str(random.getrandbits(63))
    request_id = random.randint(1, 60000)
    body = lightspeed.build_send_body(
        thread_id=args.thread,
        text=args.text,
        otid=otid,
        request_id=request_id,
        version_id=version_id,
        epoch_id=random.getrandbits(63),
        data_trace_id="#" + uuid.uuid4().hex[:22],
    )
    frame = lightspeed.encode_send(body, request_id)

    print(f"url       : {gateway_url(uid)[:110]}…")
    print(f"otid      : {otid}")
    print(f"request_id: {request_id}")
    print(f"frame     : {len(frame)} bytes ({frame[:16].hex(' ')})")
    if args.dry_run:
        print(json.dumps(json.loads(body["payload"]), indent=2)[:1200])
        return 0

    try:
        import websocket
    except ImportError:
        raise SystemExit("websocket-client is required: pip install websocket-client") from None

    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    ws = websocket.create_connection(
        gateway_url(uid),
        timeout=args.seconds,
        origin="https://www.facebook.com",
        header=[f"Cookie: {cookie_header}", f"User-Agent: {USER_AGENT}"],
    )
    try:
        if args.warmup:
            for index, tasks in enumerate(lightspeed.warmup_task_batches(args.thread, uid)):
                envelope = lightspeed.build_envelope(
                    tasks, request_id=10 + index, version_id=version_id
                )
                ws.send_binary(lightspeed.encode_send(envelope, 10 + index))
                time.sleep(0.03)
        ws.send_binary(frame)
        print("sent; reading replies…")
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except Exception as exc:  # noqa: BLE001 - a closed stream is an outcome
                print(f"stream ended: {type(exc).__name__}")
                break
            data = raw if isinstance(raw, bytes) else raw.encode()
            text = data.decode("utf-8", "replace")
            if otid in text:
                verdict = "REJECTED" if "markOptimisticMessageFailed" in text else "accepted?"
                print(f"reply ({verdict}): {text[:300]}")
                return 0
            if "Couldn't send" in text or "Failed" in text:
                print(f"reply (error): {text[:300]}")
    finally:
        ws.close()

    print("no verdict mentioning our otid; run tools/sniff.py --websocket to compare")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
