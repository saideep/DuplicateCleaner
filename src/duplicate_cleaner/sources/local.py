"""LocalFileSystemSource — thin facade over scan.walk + apply.mover/undo.

Sub-milestone 1 of v0.2 introduces the Source abstraction without changing
any behavior of the existing pipeline. The class delegates to the still-
authoritative helpers in ``scan/walk.py``, ``apply/mover.py``, and
``apply/undo.py`` so all 148 v0.1.1 tests keep passing byte-identically.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import send2trash  # type: ignore[import-untyped]

from duplicate_cleaner.paths import trash_dir_for
from duplicate_cleaner.scan.walk import FileRecord, WalkStats, iter_files
from duplicate_cleaner.sources.base import SourceMetadata, TrashedLocation

TrashFn = Callable[[Path], Path | None]


def _default_local_trash_fn(path: Path) -> Path | None:
    """Trash ``path`` via ``send2trash`` and best-effort record the destination.

    Mirrors ``apply.mover._default_trash_fn``: snapshot the trash dir before
    and after so the caller can stamp a specific ``trashed_at_path`` into the
    manifest when unambiguous.  On ambiguity (0 or >1 new entries) returns
    ``None`` so undo falls back to a basename+hash scan.
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


class LocalFileSystemSource:
    """Source implementation for the local filesystem — the only source in v0.1.

    ``is_read_only_scan`` defaults True as a defense-in-depth tripwire: a
    scan-time construction can never trash a file even if the scan code path
    accidentally calls ``move_to_trash``.  ``apply/mover.py`` will construct
    the source with the flag off (wired in sub-phase 5).
    """

    id: ClassVar[str] = "local"

    def __init__(
        self,
        roots: list[Path] | None = None,
        *,
        follow_symlinks: bool = False,
        exclude_globs: list[str] | None = None,
        min_size_bytes: int = 0,
        bundle_extensions: tuple[str, ...] | list[str] | None = None,
        stats: WalkStats | None = None,
        is_read_only_scan: bool = True,
        trash_fn: TrashFn | None = None,
    ) -> None:
        self.roots: list[Path] = list(roots) if roots else []
        self.follow_symlinks = follow_symlinks
        self.exclude_globs = list(exclude_globs) if exclude_globs else []
        self.min_size_bytes = min_size_bytes
        self.bundle_extensions = bundle_extensions
        self.stats = stats
        self.is_read_only_scan = is_read_only_scan
        self._trash_fn: TrashFn = trash_fn or _default_local_trash_fn

    def list_files(self) -> Iterator[FileRecord]:
        """Yield walker records for every configured root."""
        return iter_files(
            self.roots,
            follow_symlinks=self.follow_symlinks,
            exclude_globs=self.exclude_globs,
            min_size_bytes=self.min_size_bytes,
            bundle_extensions=self.bundle_extensions,
            stats=self.stats,
        )

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        """Stream ``record.path`` in ``chunk_size`` pieces."""
        with record.path.open("rb") as f:
            while True:
                buf = f.read(chunk_size)
                if not buf:
                    break
                yield buf

    def move_to_trash(self, record: FileRecord) -> TrashedLocation:
        """Send ``record.path`` to the volume-appropriate Trash directory."""
        if self.is_read_only_scan:
            raise PermissionError(
                f"LocalFileSystemSource(id={self.id!r}) is read-only; "
                "construct with is_read_only_scan=False to enable trashing."
            )
        dest = self._trash_fn(record.path)
        return TrashedLocation(
            source_id=self.id,
            original_path=str(record.path),
            cloud_file_id=None,
            cloud_trash_id=None,
            local_trashed_at_path=dest,
        )

    def restore_from_trash(self, loc: TrashedLocation) -> None:
        """Move a previously-trashed file back to its recorded ``original_path``.

        Sub-milestone 1 keeps the CLI ``dc undo`` path going through
        ``apply.undo.restore_from_manifest``; this method exists to satisfy
        the Source protocol and will be wired into the mover/undo dispatch
        in sub-phase 5.  Full poisoned-manifest validation lives in
        ``restore_from_manifest``; callers of this low-level entry point are
        expected to have already validated both endpoints.
        """
        src = loc.local_trashed_at_path
        if src is None:
            raise FileNotFoundError(
                "TrashedLocation has no local_trashed_at_path; "
                "cannot restore local file."
            )
        if not src.exists():
            raise FileNotFoundError(f"Trashed file missing: {src}")
        # Delegate the actual move to apply.undo so shutil.move stays in the
        # one file the forbidden-calls whitelist names.  Import lazily to
        # avoid a hard cycle if apply.undo later imports sources.
        from duplicate_cleaner.apply.undo import local_restore

        local_restore(src, Path(loc.original_path))

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        """Return an empty SourceMetadata — local files carry no cloud fields."""
        return SourceMetadata()
