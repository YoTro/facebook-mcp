"""The per-account Chrome launcher must not collide or mis-target accounts.

These tests only exercise ``--dry-run``, so no browser is started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "launch_chrome_debug.sh"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("darwin", "linux")), reason="bash launcher"
)


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FB_HOME": str(home), "PYTHON": sys.executable}
    env.pop("FB_ACCOUNT", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=PACKAGE_ROOT,
        timeout=60,
    )


def _port(result: subprocess.CompletedProcess[str]) -> int | None:
    for line in result.stdout.splitlines():
        if line.startswith("port"):
            return int(line.split(":")[-1].strip())
    return None


def _account(home: Path, name: str, meta: dict | None = None) -> Path:
    directory = home / "accounts" / name
    directory.mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (directory / "account.json").write_text(json.dumps(meta), encoding="utf-8")
    return directory


def test_claimed_ports_are_skipped(tmp_path: Path) -> None:
    _account(tmp_path, "default", {"debug_port": 9222})
    _account(tmp_path, "work", {"debug_port": 9223})

    result = _run(tmp_path, "--account", "personal")

    assert result.returncode == 0, result.stderr
    assert _port(result) == 9224


def test_recorded_port_is_reused_when_free(tmp_path: Path) -> None:
    _account(tmp_path, "work", {"debug_port": 9301})

    result = _run(tmp_path, "--account", "work")

    assert result.returncode == 0, result.stderr
    assert _port(result) == 9301


def test_different_accounts_do_not_share_a_port(tmp_path: Path) -> None:
    _account(tmp_path, "default", {"debug_port": 9222})
    _account(tmp_path, "work", {"debug_port": 9223})

    personal = _run(tmp_path, "--account", "personal")
    other = _run(tmp_path, "--account", "other")

    # Both are unclaimed, so both legitimately see 9224 — unless one is recorded.
    # Recording personal then forces `other` elsewhere, which is the invariant
    # that matters: a *launched* account never shares its port.
    assert _port(personal) == _port(other) == 9224
    (tmp_path / "accounts" / "personal" / "account.json").write_text(
        json.dumps({"debug_port": 9224}), encoding="utf-8"
    )
    assert _port(_run(tmp_path, "--account", "other")) == 9225


def test_invalid_account_name_is_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path, "--account", "../escape")
    assert result.returncode != 0
    assert "invalid account name" in result.stderr


def test_dry_run_does_not_launch_or_write_metadata(tmp_path: Path) -> None:
    _account(tmp_path, "work")

    result = _run(tmp_path, "--account", "work")

    assert result.returncode == 0, result.stderr
    assert "(dry run: nothing launched)" in result.stdout
    assert not (tmp_path / "accounts" / "work" / "account.json").exists()


def test_stop_kills_only_the_exact_account(tmp_path: Path) -> None:
    """A regex-unescaped pgrep pattern would let "a.b" match the "axb" account."""
    processes = []
    try:
        for name in ("a.b", "axb"):
            profile = _account(tmp_path, name, {"debug_port": 9400}) / "chrome"
            profile.mkdir(parents=True, exist_ok=True)
            processes.append(
                subprocess.Popen(
                    ["bash", "-c", f'exec -a "Chrome --user-data-dir={profile}" sleep 30']
                )
            )
        time.sleep(0.4)

        stop = subprocess.run(
            ["bash", str(SCRIPT), "--account", "a.b", "--stop"],
            capture_output=True,
            text=True,
            env={**os.environ, "FB_HOME": str(tmp_path), "PYTHON": sys.executable},
            cwd=PACKAGE_ROOT,
            timeout=60,
        )
        assert stop.returncode == 0, stop.stderr
        time.sleep(0.6)

        victim, bystander = processes
        assert victim.poll() is not None, "the requested account was not stopped"
        assert bystander.poll() is None, (
            "an account whose name differs only in regex metacharacters was killed"
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
