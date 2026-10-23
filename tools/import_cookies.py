#!/usr/bin/env python
"""Import cookies from a local Chrome profile.

Chrome on macOS stores cookies encrypted with AES-128-CBC under a key derived
from the *Chrome Safe Storage* Keychain entry (scheme ``v10``). This tool
decrypts the cookies for a given domain and writes a Netscape ``cookies.txt``
plus a ready-to-use ``Cookie:`` header, so the pure-HTTP transport can act as
your logged-in session — no 13 GB profile copy and no manual export.

It only touches **your own** local profile on **your own** machine.

Usage::

    .venv/bin/python tools/import_cookies.py                  # -> ~/.facebook-mcp/accounts/default
    .venv/bin/python tools/import_cookies.py --account work
    .venv/bin/python tools/import_cookies.py --print-header   # just the Cookie header

macOS may ask for Keychain access the first time.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facebook_mcp.core.accounts import AccountPaths

CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
SAFE_STORAGE_SERVICE = "Chrome Safe Storage"
SAFE_STORAGE_ACCOUNT = "Chrome"
PBKDF2_SALT = b"saltysalt"
PBKDF2_ITERATIONS = 1003
KEY_LENGTH = 16
IV = b" " * 16


def chrome_profile_dir(profile: str) -> Path:
    return CHROME_ROOT / profile


def read_safe_storage_key() -> bytes:
    """Derive the AES key from the macOS Keychain entry."""
    try:
        password = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                SAFE_STORAGE_SERVICE,
                "-a",
                SAFE_STORAGE_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=25,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "cannot read 'Chrome Safe Storage' from the Keychain "
            f"({exc.stderr.strip() or exc}). Grant access when macOS prompts."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            "timed out reading the Keychain. macOS may be waiting for you to "
            "approve Keychain access — try again and click Allow."
        ) from exc

    if not password:
        raise SystemExit("Keychain returned an empty 'Chrome Safe Storage' password")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=KEY_LENGTH,
        salt=PBKDF2_SALT,
        iterations=PBKDF2_ITERATIONS,
    ).derive(password.encode("utf-8"))


def decrypt_value(encrypted: bytes, key: bytes, host_key: str) -> str | None:
    """Decrypt a single Chrome cookie value (``v10``/``v11`` scheme)."""
    if not encrypted:
        return ""
    if not encrypted.startswith((b"v10", b"v11")):
        # Already plaintext (older Chrome / manually inserted).
        try:
            return encrypted.decode("utf-8")
        except UnicodeDecodeError:
            return None

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
    padded = decryptor.update(encrypted[3:]) + decryptor.finalize()

    if not padded:
        return None
    pad_len = padded[-1]
    if not 1 <= pad_len <= 16 or pad_len > len(padded):
        return None
    plain = padded[:-pad_len]

    # Chrome >= 80 prefixes a SHA256(host_key) digest (DB schema version >= 24).
    digest = hashlib.sha256(host_key.encode("utf-8")).digest()
    if plain.startswith(digest):
        plain = plain[len(digest) :]

    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_cookies(profile_dir: Path, domain: str) -> list[dict[str, str]]:
    """Read and decrypt cookies matching ``domain`` from the profile."""
    db_path = profile_dir / "Cookies"
    if not db_path.is_file():
        db_path = profile_dir / "Network" / "Cookies"
    if not db_path.is_file():
        raise SystemExit(f"cookie database not found under {profile_dir}")

    key = read_safe_storage_key()

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "Cookies"
        shutil.copy2(db_path, snapshot)  # avoid locking the live database

        connection = sqlite3.connect(snapshot)
        try:
            rows = connection.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly FROM cookies WHERE host_key LIKE ?",
                (f"%{domain}%",),
            ).fetchall()
        finally:
            connection.close()

    cookies: list[dict[str, str]] = []
    for host_key, name, value, encrypted, path, expires, secure, httponly in rows:
        decoded = value or decrypt_value(encrypted or b"", key, host_key)
        if decoded is None:
            print(f"  ! could not decrypt {name}", file=sys.stderr)
            continue
        cookies.append(
            {
                "domain": host_key,
                "name": name,
                "value": decoded,
                "path": path or "/",
                "expires": str(expires or 0),
                "secure": "TRUE" if secure else "FALSE",
                "httponly": "TRUE" if httponly else "FALSE",
            }
        )
    return cookies


def write_netscape(cookies: list[dict[str, str]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", "# Generated by facebook-mcp/tools/import_cookies.py"]
    for cookie in cookies:
        lines.append(
            "\t".join(
                [
                    cookie["domain"],
                    "TRUE" if cookie["domain"].startswith(".") else "FALSE",
                    cookie["path"],
                    cookie["secure"],
                    cookie["expires"],
                    cookie["name"],
                    cookie["value"],
                ]
            )
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_import(paths: AccountPaths, cookies: list[dict[str, str]]) -> None:
    """Mark the jar as user-supplied so invalidate() never deletes it.

    A previous run may have persisted a browser bootstrap and left
    ``source="cdp-bootstrap"`` behind; without this the freshly imported file
    would still look like a cache this process owns, and an auth failure would
    delete a jar the user cannot reproduce in-process.
    """
    values = {cookie["name"]: cookie["value"] for cookie in cookies}
    authenticated = {"c_user", "xs"} <= set(values)
    paths.merge_meta(
        c_user=values.get("c_user") if authenticated else None,
        authenticated=authenticated,
        updated_at=int(time.time()),
        source="import",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Chrome cookies for a domain")
    parser.add_argument("--domain", default="facebook.com")
    parser.add_argument("--profile", default="Default", help="Chrome profile dir name")
    parser.add_argument("--account", default="default", help="account name")
    parser.add_argument("--home", default=None, help="credential root (~/.facebook-mcp)")
    parser.add_argument("--out", default=None, help="explicit output path override")
    parser.add_argument("--print-header", action="store_true", help="print Cookie: header only")
    args = parser.parse_args(argv)

    profile_dir = chrome_profile_dir(args.profile)
    print(f"profile : {profile_dir}")
    cookies = read_cookies(profile_dir, args.domain)
    print(f"cookies : {len(cookies)} for {args.domain}")

    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    if args.print_header:
        print(header)
        return 0

    if args.out:
        write_netscape(cookies, Path(args.out))
        Path(args.out).chmod(0o600)
        print(f"written : {args.out} (mode 600)")
    else:
        paths = AccountPaths.for_account(args.account, args.home).ensure()
        write_netscape(cookies, paths.cookies_file)
        paths.cookies_file.chmod(0o600)
        record_import(paths, cookies)
        print(f"written : {paths.cookies_file} (mode 600)")

    names = {c["name"] for c in cookies}
    if {"c_user", "xs"} <= names:
        print("\nlogged-in Facebook cookies found (c_user + xs).")
        print("Run without a browser:")
        print(f"  FB_TRANSPORT_MODE=http FB_ACCOUNT={args.account} .venv/bin/facebook-mcp")
        c_user = next(c["value"] for c in cookies if c["name"] == "c_user")
        print(f"c_user = {c_user}")
    else:
        print("\nwarning: no c_user/xs pair — this profile is not logged in to Facebook.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
