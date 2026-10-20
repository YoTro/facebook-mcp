"""Credential storage layout (L0 core layer).

All credentials live **outside the repository** so they never end up in a git
diff, a Docker image, or a screen-share:

    ~/.facebook-mcp/                     # 0700
    └── accounts/
        ├── default/
        │   ├── cookies.txt              # 0600, Netscape format
        │   └── account.json             # 0600, metadata + per-account overrides
        └── work/
            ├── cookies.txt
            └── account.json

``FB_HOME`` overrides the root (handy for tests / containers);
``FB_ACCOUNT`` selects which account directory is active.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path.home() / ".facebook-mcp"
DEFAULT_ACCOUNT = "default"

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DIR_MODE = 0o700
FILE_MODE = 0o600


class AccountNameError(ValueError):
    """The account name is not a safe directory name."""


def resolve_home(explicit: str | Path | None = None) -> Path:
    """Resolve the credential root.

    Priority: explicit argument → ``FB_HOME`` → ``~/.facebook-mcp``.
    """
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get("FB_HOME")
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_HOME


def validate_account_name(name: str) -> str:
    """Return ``name`` if it is a safe directory name, else raise."""
    name = (name or "").strip()
    if not ACCOUNT_NAME_RE.match(name):
        raise AccountNameError(
            f"invalid account name {name!r}: use letters, digits, '.', '_' or '-' "
            "(must start with a letter or digit, max 64 chars)"
        )
    return name


@dataclass(frozen=True)
class AccountPaths:
    """Filesystem layout for one account."""

    home: Path
    name: str

    def __post_init__(self) -> None:
        validate_account_name(self.name)

    @classmethod
    def for_account(cls, account: str, home: str | Path | None = None) -> AccountPaths:
        return cls(home=resolve_home(home), name=validate_account_name(account))

    @property
    def directory(self) -> Path:
        return self.home / "accounts" / self.name

    @property
    def cookies_file(self) -> Path:
        return self.directory / "cookies.txt"

    @property
    def meta_file(self) -> Path:
        return self.directory / "account.json"

    # --- lifecycle ------------------------------------------------------

    def exists(self) -> bool:
        return self.cookies_file.is_file() or self.meta_file.is_file()

    def ensure(self) -> AccountPaths:
        """Create the directory tree with restrictive permissions."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        _restrict(self.home, DIR_MODE)
        _restrict(self.home / "accounts", DIR_MODE)
        _restrict(self.directory, DIR_MODE)
        return self

    # --- metadata -------------------------------------------------------

    def load_meta(self) -> dict[str, Any]:
        if not self.meta_file.is_file():
            return {}
        try:
            data = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_meta(self, data: dict[str, Any]) -> None:
        self.ensure()
        self.meta_file.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _restrict(self.meta_file, FILE_MODE)

    def merge_meta(self, **updates: Any) -> dict[str, Any]:
        merged = {**self.load_meta(), **updates}
        self.save_meta(merged)
        return merged

    # --- diagnostics ----------------------------------------------------

    def permission_warning(self) -> str | None:
        """Return a warning when the cookie file is readable by others."""
        if not self.cookies_file.is_file():
            return None
        mode = stat.S_IMODE(self.cookies_file.stat().st_mode)
        if mode & 0o077:
            return (
                f"{self.cookies_file} is readable by other users "
                f"(mode {mode:o}); expected 600. Fix with: chmod 600 {self.cookies_file}"
            )
        return None


def list_accounts(home: str | Path | None = None) -> list[str]:
    """Return the account names that have a directory on disk."""
    root = resolve_home(home) / "accounts"
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and ACCOUNT_NAME_RE.match(child.name):
            names.append(child.name)
    return names


def _restrict(path: Path, mode: int) -> None:
    """Best-effort chmod (ignored on platforms without POSIX modes)."""
    with contextlib.suppress(OSError):
        path.chmod(mode)
