"""``accounts.toml`` registry — one row per registered cloud account.

Kept intentionally minimal: the token file (see ``tokens.py``) is the source
of truth for credentials.  This file is a discoverability index the CLI
reads for ``dc auth list`` / ``dc sources list`` and to translate a
``--sources gdrive:personal`` flag into a concrete :class:`Source`.
"""
from __future__ import annotations

import logging
import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "duplicate_cleaner"
ACCOUNTS_PATH = CONFIG_DIR / "accounts.toml"

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_SCHEMA_VERSION = 1


class DuplicateAccountError(ValueError):
    """Raised when adding an account with an id already in the registry."""


@dataclass(frozen=True)
class AccountEntry:
    """A single registered cloud account."""

    id: str
    type: str
    label: str
    user: str
    added_ts: str

    def to_dict(self) -> dict[str, Any]:
        """Return the TOML-serialisable representation."""
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "user": self.user,
            "added_ts": self.added_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountEntry:
        """Construct from a TOML dict; missing optional fields default."""
        return cls(
            id=str(data["id"]),
            type=str(data.get("type", "")),
            label=str(data.get("label", "")),
            user=str(data.get("user", "")),
            added_ts=str(data.get("added_ts", "")),
        )


class AccountsRegistry:
    """Read/write ``accounts.toml``, enforcing unique ids."""

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = Path(path) if path is not None else ACCOUNTS_PATH

    @property
    def path(self) -> Path:
        """Return the on-disk accounts.toml path."""
        return self._path

    def load(self) -> list[AccountEntry]:
        """Return every registered account (empty list if the file is absent)."""
        if not self._path.exists():
            return []
        with self._path.open("rb") as fh:
            data: dict[str, Any] = tomllib.load(fh)
        raw = data.get("accounts", [])
        out: list[AccountEntry] = []
        if isinstance(raw, list):
            for row in raw:
                if isinstance(row, dict) and "id" in row:
                    out.append(AccountEntry.from_dict(row))
        return out

    def add(self, entry: AccountEntry) -> None:
        """Append an account, rejecting a duplicate id."""
        current = self.load()
        if any(e.id == entry.id for e in current):
            raise DuplicateAccountError(
                f"Account with id {entry.id!r} is already registered."
            )
        current.append(entry)
        self._write(current)

    def remove(self, account_id: str) -> bool:
        """Remove an account by id; returns True if a row was removed."""
        current = self.load()
        filtered = [e for e in current if e.id != account_id]
        if len(filtered) == len(current):
            return False
        self._write(filtered)
        return True

    def get(self, account_id: str) -> AccountEntry | None:
        """Return the entry with the given id, or None."""
        for e in self.load():
            if e.id == account_id:
                return e
        return None

    def _write(self, entries: list[AccountEntry]) -> None:
        """Atomically overwrite the registry at 0o600 mode."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, _DIR_MODE)
        except OSError:  # pragma: no cover - defensive
            log.debug("Cannot chmod %s to 0o700", parent)
        payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "accounts": [e.to_dict() for e in entries],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=".accounts-", suffix=".toml.tmp", dir=str(parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(tomli_w.dumps(payload).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_path, _FILE_MODE)
            os.replace(tmp_path, self._path)
            try:
                dir_fd = os.open(str(parent), os.O_RDONLY)
            except OSError:
                dir_fd = -1
            if dir_fd >= 0:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            if tmp_path.exists():
                try:
                    import send2trash  # type: ignore[import-untyped]

                    send2trash.send2trash(str(tmp_path))
                except OSError:
                    log.debug("Failed to trash leaked accounts tempfile %s", tmp_path)
            raise

    @staticmethod
    def now_ts() -> str:
        """Return a UTC ISO-8601 timestamp for the ``added_ts`` field."""
        return datetime.now(UTC).isoformat()

    def enforce_secure_mode(self) -> None:
        """Refuse to proceed if accounts.toml is world/group-readable."""
        if not self._path.exists():
            return
        mode_bits = stat.S_IMODE(self._path.stat().st_mode)
        if mode_bits & 0o077:
            raise PermissionError(
                f"accounts.toml at {self._path} has mode {oct(mode_bits)}; "
                f"must be 0o600.  Run: chmod 600 {self._path}"
            )
