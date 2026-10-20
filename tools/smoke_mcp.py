#!/usr/bin/env python
"""Smoke-test the MCP server over stdio (no network, no credentials).

Spawns ``python -m facebook_mcp``, performs the JSON-RPC handshake, then checks
that every tool is registered, documented and exposes the ``account`` argument.

Used by CI and available locally:

    .venv/bin/python tools/smoke_mcp.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

EXPECTED_TOOLS = {
    "get_user_latest_post_time",
    "get_user_recent_posts",
    "search_people",
    "search_pages",
    "send_message",
}

TIMEOUT = 40


def _messages(stdout: str) -> dict[int, dict]:
    replies: dict[int, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        message_id = message.get("id")
        if isinstance(message_id, int):
            replies[message_id] = message
    return replies


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
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"

    env = {**os.environ, "FB_HOME": os.environ.get("FB_HOME", "/tmp/facebook-mcp-smoke")}
    env.setdefault("FB_TRANSPORT_MODE", "http")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "facebook_mcp"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"timed out after {TIMEOUT}s waiting for the MCP server", file=sys.stderr)
        return 1

    replies = _messages(proc.stdout)
    if 1 not in replies:
        print("no initialize reply", file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        return 1

    info = replies[1]["result"]["serverInfo"]
    if info.get("name") != "facebook-mcp":
        print(f"unexpected serverInfo: {info}", file=sys.stderr)
        return 1

    if 2 not in replies:
        print("no tools/list reply", file=sys.stderr)
        return 1

    tools = replies[2]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    missing = EXPECTED_TOOLS - names
    if missing:
        print(f"missing tools: {sorted(missing)}", file=sys.stderr)
        return 1

    for tool in tools:
        if not tool.get("description"):
            print(f"{tool['name']} has no description", file=sys.stderr)
            return 1
        properties = tool["inputSchema"].get("properties", {})
        if "account" not in properties:
            print(f"{tool['name']} is missing the account argument", file=sys.stderr)
            return 1
        if "account" in tool["inputSchema"].get("required", []):
            print(f"{tool['name']}: account must stay optional", file=sys.stderr)
            return 1

    print(f"OK  server={info['name']} {info['version']}  tools={len(tools)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
