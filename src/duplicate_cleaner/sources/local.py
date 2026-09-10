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

from duplicate_cleaner.apply.trash import default_trash_fn as _default_local_trash_fn
from duplicate_cleaner.scan.walk import FileRecord, WalkStats, iter_files
from duplicate_cleaner.sources.base import SourceMetadata, TrashedLocation, UploadResult

TrashFn = Callable[[Path], Path | None]


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
        allowed_trash_dirs: list[Path] | None = None,
    ) -> None:
        self.roots: list[Path] = list(roots) if roots else []
        self.follow_symlinks = follow_symlinks
        self.exclude_globs = list(exclude_globs) if exclude_globs else []
        self.min_size_bytes = min_size_bytes
        self.bundle_extensions = bundle_extensions
        self.stats = stats
        self.is_read_only_scan = is_read_only_scan
        self._trash_fn: TrashFn = trash_fn or _default_local_trash_fn
        # B9: mirror ``restore_from_manifest``'s ``allowed_trash_dirs`` override
        # so tests can restore from a temp-directory Trash without touching
        # ``~/.Trash``.  Real callers leave this None; ``local_restore`` then
        # uses ``paths.known_trash_dirs()``.
        self._allowed_trash_dirs: list[Path] | None = (
            list(allowed_trash_dirs) if allowed_trash_dirs is not None else None
        )

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

        Delegates to :func:`apply.undo.local_restore`, which runs the same
        H2/F12/H5 poisoned-manifest rails that :func:`restore_from_manifest`
        enforces (trash-containment, ``::`` rejection, EXCLUDED_ROOTS).  A
        wire-up bug in sub-phase 5 that pointed this method at ``~/.ssh``
        would trip the guard, not shutil.move ~/.ssh into a scan root.
        """
        src = loc.local_trashed_at_path
        if src is None:
            raise FileNotFoundError(
                "TrashedLocation has no local_trashed_at_path; "
                "cannot restore local file."
            )
        if not src.exists():
            raise FileNotFoundError(f"Trashed file missing: {src}")
        # Import lazily so ``apply.undo`` can freely import from ``sources``
        # in a future refactor without introducing a cycle here.
        from duplicate_cleaner.apply.undo import local_restore

        local_restore(
            src,
            Path(loc.original_path),
            allowed_trash_dirs=self._allowed_trash_dirs,
        )

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        """Return an empty SourceMetadata — local files carry no cloud fields."""
        return SourceMetadata()

    def upload(
        self,
        dest_path: str,
        byte_stream: Iterator[bytes],
        expected_size: int,
    ) -> UploadResult:
        """v0.5-a stretch goal: local upload is deferred.

        Migrations from cloud sources into a local directory are technically
        useful (pull down before deletion) but land in v0.6+ alongside the
        photo-library work.  For now the migrate planner only proposes
        cloud-to-cloud destinations; a plan that names local as its ``--to``
        target should surface at planner time, not here.
        """
        _ = (dest_path, byte_stream, expected_size)
        raise NotImplementedError(
            "Migration to local disk is not supported in v0.5; use "
            "cloud-to-cloud only."
        )

    def check_drift(self, record: FileRecord) -> None:
        """Verify the file on disk still matches ``record.size`` and ``record.mtime``.

        v0.2 sub-phase 5c: added for parity with the cloud sources so the
        mover can dispatch drift-check through the Source protocol without
        special-casing local records.  The mover's existing
        ``_verify_unchanged`` pathway is what actually runs for local
        discards in :func:`apply.mover.apply_report`; this method is here so
        a caller that wants a uniform :meth:`Source.check_drift` interface
        can use it too.  Raises :class:`FileNotFoundError` when the file is
        gone, ``ValueError`` on any (size, mtime) mismatch.
        """
        p = record.path
        if not p.exists():
            raise FileNotFoundError(f"missing on disk: {p}")
        st = p.stat()
        if st.st_size != record.size:
            raise ValueError(
                f"size changed: {p} (was {record.size}, now {st.st_size})"
            )
        if abs(st.st_mtime - record.mtime) > 1e-3:
            raise ValueError(
                f"mtime changed: {p} (was {record.mtime}, now {st.st_mtime})"
            )
