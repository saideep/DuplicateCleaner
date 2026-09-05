"""On-disk token store — atomic 0o600 writes, mode enforcement on read.

Token files hold long-lived refresh tokens.  They are treated as sensitive
key material: parent directory is chmod 0o700, files are chmod 0o600 and
verified on every read.  A file with a looser mode is rejected outright
instead of silently used — surfacing the misconfiguration once, loudly, is
safer than silently loading a world-readable token.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import send2trash  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "duplicate_cleaner"
TOKENS_DIR = CONFIG_DIR / "tokens"

_FILE_MODE = 0o600
_DIR_MODE = 0o700


class TokenPermissionError(RuntimeError):
    """Raised when a token file has a filesystem mode looser than 0o600."""


class TokenStore:
    """Persist per-account OAuth token blobs under ``TOKENS_DIR``."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._dir: Path = Path(base_dir) if base_dir is not None else TOKENS_DIR

    @property
    def directory(self) -> Path:
        """Return the underlying directory holding token files."""
        return self._dir

    def _path_for(self, account_id: str) -> Path:
        """Return the on-disk path for an account's token file."""
        if not account_id or "/" in account_id or account_id in {".", ".."}:
            raise ValueError(f"Invalid account_id for token file: {account_id!r}")
        return self._dir / f"{account_id}.json"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._dir, _DIR_MODE)
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Cannot chmod %s to 0o700: %s", self._dir, exc)

    def save(self, account_id: str, token_data: dict[str, Any]) -> None:
        """Write a token blob atomically at mode 0o600 with parent 0o700."""
        self._ensure_dir()
        dest = self._path_for(account_id)
        # Tempfile lives in the same directory so os.replace stays atomic
        # across the (destination) filesystem boundary.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tok-", suffix=".json.tmp", dir=str(self._dir)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(token_data, fh, sort_keys=True, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_path, _FILE_MODE)
            os.replace(tmp_path, dest)
            # Best-effort fsync of the parent so the rename is durable.
            try:
                dir_fd = os.open(str(self._dir), os.O_RDONLY)
            except OSError:
                dir_fd = -1
            if dir_fd >= 0:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            # send2trash the leaked tempfile so we never violate the
            # forbidden-calls invariant (no os.remove in src/).
            if tmp_path.exists():
                try:
                    send2trash.send2trash(str(tmp_path))
                except OSError:
                    log.debug("Failed to trash leaked tempfile %s", tmp_path)
            raise

    def load(self, account_id: str) -> dict[str, Any] | None:
        """Return the parsed token blob, or None if the file is absent."""
        path = self._path_for(account_id)
        if not path.exists():
            return None
        st = path.stat()
        mode_bits = stat.S_IMODE(st.st_mode)
        if mode_bits & 0o077:
            raise TokenPermissionError(
                f"Token file {path} has mode {oct(mode_bits)}; must be 0o600. "
                f"Run: chmod 600 {path}"
            )
        raw = path.read_text()
        parsed: Any = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Token file {path} is not a JSON object")
        return parsed

    def delete(self, account_id: str) -> None:
        """Remove the token file via Trash so nothing hard-deletes secrets."""
        path = self._path_for(account_id)
        if not path.exists():
            return
        send2trash.send2trash(str(path))

    def list_accounts(self) -> list[str]:
        """Enumerate token files by their stem (account_id)."""
        if not self._dir.exists():
            return []
        out: list[str] = []
        for p in sorted(self._dir.iterdir()):
            if p.is_file() and p.suffix == ".json" and not p.name.startswith("."):
                out.append(p.stem)
        return out
