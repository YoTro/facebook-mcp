#!/usr/bin/env python
"""Export the attached Chrome's cookies into an isolated account store.

Cookies are written to ``~/.facebook-mcp/accounts/<account>/cookies.txt`` with
mode ``0600`` — outside the repository, so they can never be committed.

Afterwards set ``FB_TRANSPORT_MODE=http`` and the server runs without Chrome.

Usage::

    .venv/bin/python tools/export_cookies.py
    .venv/bin/python tools/export_cookies.py --account work
    .venv/bin/python tools/export_cookies.py --list
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facebook_mcp.core.accounts import AccountPaths, list_accounts, resolve_home
from facebook_mcp.core.config import get_settings
from facebook_mcp.transport.cdp import CdpTransport


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Export Chrome cookies via CDP")
    parser.add_argument("--account", default=settings.account, help="account name")
    parser.add_argument("--home", default=settings.home, help="credential root")
    parser.add_argument("--domain", default="facebook.com")
    parser.add_argument("--url", default="https://www.facebook.com/")
    parser.add_argument("--list", action="store_true", help="list known accounts and exit")
    args = parser.parse_args(argv)

    home = resolve_home(args.home)

    if args.list:
        names = list_accounts(home)
        print(f"credential root: {home}")
        if not names:
            print("no accounts yet")
            return 0
        for name in names:
            paths = AccountPaths.for_account(name, home)
            meta = paths.load_meta()
            state = "authenticated" if paths.cookies_file.is_file() else "no cookies"
            user = meta.get("c_user") or "-"
            print(f"  {name:12s} {state:14s} c_user={user}  {paths.directory}")
        return 0

    paths = AccountPaths.for_account(args.account, home)

    transport = CdpTransport(get_settings())
    try:
        cookies = transport.cookies(args.url)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read cookies from Chrome: {exc}", file=sys.stderr)
        print("Start Chrome with: bash tools/launch_chrome_debug.sh", file=sys.stderr)
        return 1
    finally:
        transport.close()

    if not cookies:
        print(f"no cookies found for {args.url}", file=sys.stderr)
        return 1

    lines = [
        "# Netscape HTTP Cookie File",
        "# Exported by facebook-mcp/tools/export_cookies.py",
    ]
    expiry = int(time.time()) + 30 * 86400
    for name, value in sorted(cookies.items()):
        lines.append("\t".join([f".{args.domain}", "TRUE", "/", "TRUE", str(expiry), name, value]))

    paths.ensure()
    paths.cookies_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths.cookies_file.chmod(0o600)

    authenticated = {"c_user", "xs"} <= set(cookies)
    paths.merge_meta(
        c_user=cookies.get("c_user") if authenticated else None,
        authenticated=authenticated,
        updated_at=int(time.time()),
        source="cdp-export",
    )

    print(f"account : {paths.name}")
    print(f"written : {paths.cookies_file} (mode 600)")
    if authenticated:
        print(f"logged in as c_user={cookies['c_user']}")
        print("\nRun without a browser:")
        print(f"  FB_TRANSPORT_MODE=http FB_ACCOUNT={paths.name} .venv/bin/facebook-mcp")
    else:
        print("warning: c_user/xs missing — Chrome is not logged in to Facebook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
