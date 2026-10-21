"""The stdio smoke test must not deadlock on a chatty server.

``tools/smoke_mcp.py`` pipes both stdout and stderr. Reading stderr only after
``wait()`` lets a child that writes more than the pipe buffer (~64 KB) block on
write, which surfaces as a bogus "timed out waiting for initialize reply".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import smoke_mcp

REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


def _stub(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def test_chatty_stderr_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 KB of stderr before the replies: both pipes must be drained."""
    monkeypatch.setattr(smoke_mcp, "TIMEOUT", 10)
    body = (
        "import json, sys\n"
        "sys.stderr.write('x' * 200_000)\n"
        "sys.stderr.flush()\n"
        "for req in ({'id': 1}, {'id': 2}):\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {}}), flush=True)\n"
    )
    replies, stderr = smoke_mcp._run(REQUESTS, dict(), _stub(body))

    assert set(replies) == {1, 2}, "replies were lost behind the unread stderr pipe"
    assert len(stderr) >= 200_000


def test_replies_are_collected_before_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that never exits must still be usable (stdin stays open)."""
    monkeypatch.setattr(smoke_mcp, "TIMEOUT", 10)
    body = (
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if 'id' in req:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {}}), flush=True)\n"
        "# keep running until the client closes stdin (EOF), then exit cleanly\n"
    )
    replies, _ = smoke_mcp._run(REQUESTS, dict(), _stub(body))
    assert set(replies) == {1, 2}


def test_stderr_is_reported_on_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A crashing server: the traceback reaches the operator."""
    monkeypatch.setattr(smoke_mcp, "_run", lambda *a, **k: ({}, "boom-traceback\n"))
    exit_code = smoke_mcp.main()
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "boom-traceback" in captured.err
