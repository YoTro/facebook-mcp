#!/usr/bin/env python
"""Smoke-test the MCP server over stdio (no network, no credentials).

Spawns ``python -m facebook_mcp``, performs the JSON-RPC handshake, then checks
that every tool is registered, documented and exposes the ``account`` argument.

**Why not ``subprocess.run(input=...)``**: that closes stdin right after the
payload is written, and the SDK cancels in-flight handlers when the read stream
ends (``mcp/server/lowlevel/server.py``: ``finally: tg.cancel_scope.cancel()``).
``tools/list`` has to build a JSON schema per tool, so on a slower machine the
EOF wins and the reply never lands — the CI failure mode was exactly
``no tools/list reply``. A real client keeps the session open; this one does
too, and only closes stdin once the replies are in.

Used by CI and available locally:

    .venv/bin/python tools/smoke_mcp.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time

EXPECTED_TOOLS = {
    "get_user_latest_post_time",
    "get_user_recent_posts",
    "search_people",
    "search_pages",
    "send_message",
}

TIMEOUT = 40


def _reply(line: str) -> tuple[int, dict] | None:
    """Parse one stdout line into ``(id, message)`` when it is a JSON-RPC reply."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        message = json.loads(line)
    except ValueError:
        return None
    message_id = message.get("id")
    if not isinstance(message_id, int):
        return None
    return message_id, message


def _run(requests: list[dict], env: dict[str, str]) -> tuple[dict[int, dict], str]:
    """Send ``requests`` to a fresh server and collect the JSON-RPC replies."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "facebook_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    replies: dict[int, dict] = {}
    expected = {r["id"] for r in requests if "id" in r}
    assert proc.stdout is not None and proc.stdin is not None and proc.stderr is not None

    def pump() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            parsed = _reply(line)
            if parsed is not None:
                replies[parsed[0]] = parsed[1]

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline and not expected <= replies.keys():
            if proc.poll() is not None:  # the server died: stop waiting
                break
            reader.join(0.05)
    finally:
        # EOF only after the replies are in; see the module docstring.
        with contextlib.suppress(OSError):
            proc.stdin.close()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - misbehaving server
        proc.kill()
        proc.wait(timeout=5)
    reader.join(2)
    return replies, proc.stderr.read()


def main() -> int:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]

    env = {**os.environ, "FB_HOME": os.environ.get("FB_HOME", "/tmp/facebook-mcp-smoke")}
    env.setdefault("FB_TRANSPORT_MODE", "http")

    started = time.monotonic()
    replies, stderr = _run(requests, env)
    elapsed = time.monotonic() - started

    def fail(message: str) -> int:
        print(message, file=sys.stderr)
        tail = stderr[-2000:].rstrip()
        if tail:
            print(tail, file=sys.stderr)
        return 1

    if 1 not in replies:
        reason = "timed out waiting for" if elapsed >= TIMEOUT else "no"
        return fail(f"{reason} initialize reply (server finished in {elapsed:.1f}s)")

    info = replies[1]["result"]["serverInfo"]
    if info.get("name") != "facebook-mcp":
        return fail(f"unexpected serverInfo: {info}")

    if 2 not in replies:
        reason = "timed out waiting for" if elapsed >= TIMEOUT else "no"
        return fail(f"{reason} tools/list reply (server finished in {elapsed:.1f}s)")

    if "result" not in replies[2]:
        return fail(f"tools/list returned an error: {replies[2].get('error')}")

    tools = replies[2]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    missing = EXPECTED_TOOLS - names
    if missing:
        return fail(f"missing tools: {sorted(missing)}")

    for tool in tools:
        if not tool.get("description"):
            return fail(f"{tool['name']} has no description")
        properties = tool["inputSchema"].get("properties", {})
        if "account" not in properties:
            return fail(f"{tool['name']} is missing the account argument")
        if "account" in tool["inputSchema"].get("required", []):
            return fail(f"{tool['name']}: account must stay optional")

    print(f"OK  server={info['name']} {info['version']}  tools={len(tools)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
