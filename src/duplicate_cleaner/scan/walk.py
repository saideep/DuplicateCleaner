"""Filesystem walk — yields FileRecords, applies hard-coded and user exclusions."""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from duplicate_cleaner.constants import EXCLUDED_DIR_NAMES, EXCLUDED_FILE_SUFFIXES
from duplicate_cleaner.paths import (
    is_excluded_root_path,
    matches_globs,
    resolve_for_check,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRecord:
    """Stat snapshot of one candidate file."""

    path: Path
    size: int
    mtime: float
    inode: int
    dev: int
    nlink: int


def iter_files(
    roots: list[Path],
    *,
    follow_symlinks: bool = False,
    exclude_globs: list[str] | None = None,
    min_size_bytes: int = 0,
) -> Iterator[FileRecord]:
    """Walk each root, yielding FileRecords for eligible regular files."""
    globs = exclude_globs or []
    seen_roots: set[Path] = set()
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        if root in seen_roots:
            continue
        seen_roots.add(root)
        if not root.exists():
            log.warning("Skipping missing root: %s", root)
            continue
        yield from _walk(root, follow_symlinks, globs, min_size_bytes)


def _walk(
    root: Path,
    follow_symlinks: bool,
    globs: list[str],
    min_size_bytes: int,
) -> Iterator[FileRecord]:
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        # Always check both the raw and physically-resolved path so a symlink
        # into an excluded root cannot bypass the check.
        if is_excluded_root_path(current) or is_excluded_root_path(
            resolve_for_check(current)
        ):
            continue
        try:
            scandir_ctx = os.scandir(current)
        except OSError as e:
            log.debug("scandir failed on %s: %s", current, e)
            continue
        with scandir_ctx as it:
            for entry in it:
                rec = _process_entry(
                    entry, stack, follow_symlinks, globs, min_size_bytes
                )
                if rec is not None:
                    yield rec


def _process_entry(
    entry: os.DirEntry[str],
    stack: list[Path],
    follow_symlinks: bool,
    globs: list[str],
    min_size_bytes: int,
) -> FileRecord | None:
    try:
        p = Path(entry.path)
        name = entry.name
        is_symlink = entry.is_symlink()
        if is_symlink and not follow_symlinks:
            return None

        # Resolve the physical path for exclusion checks (catches symlinks
        # into ~/Library, /System, etc.). Yield the un-resolved path so the
        # report reads naturally.
        resolved = resolve_for_check(p) if is_symlink else p

        if entry.is_dir(follow_symlinks=follow_symlinks):
            if name in EXCLUDED_DIR_NAMES:
                return None
            if is_excluded_root_path(p) or is_excluded_root_path(resolved):
                return None
            if matches_globs(p, globs):
                return None
            stack.append(p)
            return None
        if not entry.is_file(follow_symlinks=follow_symlinks):
            return None
        if name.endswith(EXCLUDED_FILE_SUFFIXES):
            return None
        # File-level exclusion: symlinked file pointing into excluded root.
        if is_excluded_root_path(p) or is_excluded_root_path(resolved):
            return None
        if matches_globs(p, globs):
            return None
        st = entry.stat(follow_symlinks=follow_symlinks)
        if st.st_size < min_size_bytes:
            return None
        return FileRecord(
            path=p,
            size=st.st_size,
            mtime=st.st_mtime,
            inode=st.st_ino,
            dev=st.st_dev,
            nlink=st.st_nlink,
        )
    except OSError as e:
        log.debug("Skip entry %s: %s", entry.path, e)
        return None
