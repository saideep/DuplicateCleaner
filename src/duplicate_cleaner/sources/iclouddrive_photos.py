"""iCloudPhotosSource — read the local ``Photos Library.photoslibrary`` bundle.

v0.6: iCloud Photos is scanned via the local ``osxphotos`` library, NOT via
any iCloud cloud API.  The bundle at ``~/Pictures/Photos Library.photoslibrary``
is the authoritative on-disk view of the Photos database; ``osxphotos``
enumerates its photos and gives us the local filesystem path of every
downloaded original.

Rationale: no OAuth, no cloud API surface, no rate limits.  The trade-off
is that iCloud-only stubs (photos not downloaded locally when "Optimize
Mac Storage" is enabled) surface with ``photo.path is None`` — we count
those and log the total, but do NOT emit records for them.  The user
enables "Download Originals to This Mac" in Photos → Preferences →
iCloud to make them scannable.

The source is PERMANENTLY read-only.  ``osxphotos`` is a reader library,
not a writer; the only supported deletion path is via the Photos.app
itself.  ``move_to_trash`` raises a clear ``SourceError`` pointing the
user at the Photos.app / the underlying filesystem.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    SourceDriftError,
    SourceError,
    SourceMetadata,
    SourceNotFoundError,
    TrashedLocation,
    UploadResult,
)

log = logging.getLogger(__name__)

_DEFAULT_LIBRARY_PATH = Path.home() / "Pictures" / "Photos Library.photoslibrary"


class iCloudPhotosSource:
    """Source over the local Photos.photoslibrary bundle.

    ``is_read_only_scan`` defaults True and is PERMANENT: deletion goes
    via the Photos.app, not this source.  The flag is accepted for
    interface parity with the cloud sources.
    """

    id: str
    is_read_only_scan: bool

    def __init__(
        self,
        account_id: str,
        *,
        photos_library_path: Path | None = None,
        is_read_only_scan: bool = True,
    ) -> None:
        self.id = account_id
        self._library_path: Path = (
            Path(photos_library_path)
            if photos_library_path is not None
            else _DEFAULT_LIBRARY_PATH
        )
        # v0.6: iCloud Photos is permanently read-only.  We store the flag
        # to satisfy the Source protocol contract and to let a
        # (hypothetical) future write path use the same tripwire that the
        # cloud sources do — but every write method refuses regardless of
        # this flag's value.
        self.is_read_only_scan = is_read_only_scan
        self._db: Any | None = None
        self._stub_count: int = 0

    @property
    def library_path(self) -> Path:
        """Return the ``Photos Library.photoslibrary`` bundle path in use."""
        return self._library_path

    @property
    def stub_count(self) -> int:
        """Return the number of iCloud-only stubs skipped by the last list."""
        return self._stub_count

    def _open_db(self) -> Any:
        """Return a cached ``osxphotos.PhotosDB`` for this library.

        Imported lazily so the module ``py_compile``s and the tests can run
        without ``osxphotos`` installed.  When the dep is missing we surface
        a typed ``SourceError`` with the install hint rather than a bare
        ``ImportError``.
        """
        if self._db is not None:
            return self._db
        try:
            import osxphotos  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:
            raise SourceError(
                "osxphotos is required to scan the iCloud Photos library "
                "but is not installed.  Install with `pip install "
                "'duplicate-cleaner[icloud]'`."
            ) from exc
        if not self._library_path.exists():
            raise SourceNotFoundError(
                f"Photos library not found at {self._library_path}. "
                "Pass --library-path or ensure Photos.app has run at least "
                "once."
            )
        self._db = osxphotos.PhotosDB(dbfile=str(self._library_path))
        return self._db

    def list_files(self) -> Iterator[FileRecord]:
        """Enumerate downloaded photos; skip iCloud-only stubs."""
        db = self._open_db()
        self._stub_count = 0
        for photo in db.photos():
            path_raw = getattr(photo, "path", None)
            if not path_raw:
                # ``photo.path is None`` → iCloud-only stub (not downloaded
                # to this Mac).  Count and log without yielding a record so
                # the report never contains a size=0 phantom.
                self._stub_count += 1
                continue
            local_path = Path(str(path_raw))
            try:
                size = int(local_path.stat().st_size)
            except OSError as exc:
                log.debug(
                    "iCloud Photos: skipping %s (stat failed: %s)",
                    local_path,
                    exc,
                )
                continue
            uuid = str(getattr(photo, "uuid", "") or "")
            if not uuid:
                # osxphotos always populates uuid; missing uuid is a corrupt
                # library entry — skip rather than fabricate.
                continue
            date_obj = getattr(photo, "date", None)
            mtime = 0.0
            if date_obj is not None:
                try:
                    mtime = float(date_obj.timestamp())
                except (AttributeError, OSError, ValueError):
                    mtime = 0.0
            date_modified = getattr(photo, "date_modified", None)
            # ``date_modified`` may be None for photos never edited after
            # import; fall back to a stable "unedited" marker so the etag
            # stays deterministic across scans.
            date_modified_repr = (
                date_modified.isoformat()
                if date_modified is not None and hasattr(date_modified, "isoformat")
                else "unedited"
            )
            etag = f"{uuid}:{date_modified_repr}"
            filename = str(
                getattr(photo, "original_filename", None) or local_path.name
            )
            virtual = f"iclouddrive:{self.id}://{filename}"
            yield FileRecord(
                path=Path(virtual),
                size=size,
                mtime=mtime,
                inode=0,
                dev=0,
                nlink=1,
                source_id=self.id,
                # No provider-side hash — reconciliation MUST read the local
                # bytes and BLAKE3 them.  Cached identically to Google Photos
                # by (source_id, cloud_file_id, etag).
                foreign_hash=None,
                etag=etag,
                cloud_file_id=uuid,
                # No OAuth identity → owner is unknown.  Every emitted item
                # is is_shared=False because the local library only contains
                # this user's own photos.
                owner=None,
                is_shared=False,
            )
        if self._stub_count:
            log.warning(
                "iCloud Photos: skipped %d stubs (files not downloaded "
                "locally).  Toggle 'Download Originals to This Mac' in "
                "Photos → Preferences → iCloud to make them "
                "scannable.",
                self._stub_count,
            )

    def _fetch_photo_by_uuid(self, uuid: str) -> Any:
        """Return the osxphotos ``PhotoInfo`` for ``uuid`` or raise not-found."""
        db = self._open_db()
        # osxphotos exposes ``photos_by_uuid`` on modern versions; fall back
        # to a linear scan on older versions so the source keeps working.
        lookup = getattr(db, "get_photo", None)
        if callable(lookup):
            photo = lookup(uuid)
            if photo is not None:
                return photo
        for photo in db.photos():
            if str(getattr(photo, "uuid", "")) == uuid:
                return photo
        raise SourceNotFoundError(
            f"Photos library has no item with uuid {uuid!r}."
        )

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        """Stream the local original file backing this Photos item."""
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot read bytes without cloud_file_id (uuid)"
            )
        photo = self._fetch_photo_by_uuid(record.cloud_file_id)
        path_raw = getattr(photo, "path", None)
        if not path_raw:
            raise SourceNotFoundError(
                f"{record.path}: Photos item {record.cloud_file_id!r} has "
                "no local path (iCloud-only stub)."
            )
        local_path = Path(str(path_raw))
        with local_path.open("rb") as fh:
            while True:
                buf = fh.read(chunk_size)
                if not buf:
                    break
                yield buf

    def move_to_trash(self, record: FileRecord) -> TrashedLocation:
        """Refuse — iCloud Photos deletion is done via the Photos.app."""
        _ = record
        raise SourceError(
            "iCloud Photos trash is done via the Photos.app — the "
            "source is read-only.  Use the Photos app to delete or use "
            "`dc scan` on ~/Pictures to trash the local file directly."
        )

    def restore_from_trash(self, loc: TrashedLocation) -> None:
        """Refuse — the source is permanently read-only."""
        _ = loc
        raise SourceError(
            "iCloud Photos restore is not supported — the source is "
            "read-only.  Restore via the Photos.app's Recently Deleted "
            "album."
        )

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        """Return metadata already populated at scan time — no re-fetch."""
        return SourceMetadata(
            etag=record.etag,
            cloud_file_id=record.cloud_file_id,
            owner=record.owner,
            is_shared=record.is_shared,
        )

    def check_drift(self, record: FileRecord) -> None:
        """Re-fetch date_modified and verify the composite etag matches.

        Photos edits (crop, rotate, adjust) bump ``date_modified``; a
        drift-check catches the "user edited this photo since scan" case
        before a stale deduplication decision fires.
        """
        if not record.cloud_file_id:
            raise SourceError(
                f"{record.path}: cannot check drift without cloud_file_id (uuid)"
            )
        if record.source_id != self.id:
            raise SourceError(
                f"FileRecord.source_id {record.source_id!r} does not match "
                f"this iCloudPhotosSource id {self.id!r}."
            )
        photo = self._fetch_photo_by_uuid(record.cloud_file_id)
        date_modified = getattr(photo, "date_modified", None)
        date_modified_repr = (
            date_modified.isoformat()
            if date_modified is not None and hasattr(date_modified, "isoformat")
            else "unedited"
        )
        current_etag = f"{record.cloud_file_id}:{date_modified_repr}"
        if current_etag != record.etag:
            raise SourceDriftError(
                f"Photos library drift for {record.path}: scan="
                f"{record.etag!r} now={current_etag!r} — the photo's "
                "date_modified changed since scan.  Rescan and retry."
            )

    def upload(
        self,
        dest_path: str,
        byte_stream: Iterator[bytes],
        expected_size: int,
    ) -> UploadResult:
        """Refuse — iCloud Photos is not a supported migrate destination."""
        _ = (dest_path, byte_stream, expected_size)
        raise NotImplementedError(
            "iCloud Photos upload is not supported — the source is "
            "read-only.  Use the Photos.app to import new photos."
        )
