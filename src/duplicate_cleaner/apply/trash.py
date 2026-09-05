"""Shared helper for send2trash + basename-diff destination recording.

Both :mod:`duplicate_cleaner.apply.mover` and
:mod:`duplicate_cleaner.sources.local` need to trash a file AND best-effort
record where ``send2trash`` deposited it — the destination is undo's fast
path when the trashed_at_path stamped in the manifest is unambiguous.  Two
identical implementations lived side-by-side before sub-phase 3; this module
is the single canonical spelling.
"""
from __future__ import annotations

from pathlib import Path

import send2trash  # type: ignore[import-untyped]

from duplicate_cleaner.paths import trash_dir_for


def default_trash_fn(path: Path) -> Path | None:
    """Trash ``path`` and return the resulting file inside the Trash dir.

    Snapshots the volume-appropriate Trash directory before and after
    ``send2trash`` and diffs the basename set.  Returns the single new
    entry on success; returns ``None`` on ambiguity (0 or >1 new entries)
    so callers record ``trashed_at_path=None`` and undo falls back to the
    name+hash scan in :mod:`duplicate_cleaner.apply.undo`.
    """
    trash = trash_dir_for(path)
    before: set[str] = set()
    if trash.exists():
        before = {p.name for p in trash.iterdir()}
    send2trash.send2trash(str(path))
    after: set[str] = set()
    if trash.exists():
        after = {p.name for p in trash.iterdir()}
    new = after - before
    if len(new) == 1:
        return trash / next(iter(new))
    return None
