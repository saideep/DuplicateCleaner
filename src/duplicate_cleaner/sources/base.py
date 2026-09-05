"""Source protocol + shared carrier types for pluggable file-access backends."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from duplicate_cleaner.scan.walk import FileRecord

# Algo-tagged hash string. Local records leave ``foreign_hash`` unset; cloud
# records fill it with ``"md5:..."`` / ``"sha256:..."`` etc. so the reconcile
# stage can skip a download whenever every bucket member shares an algo.
ForeignHash = str


class SourceError(Exception):
    """Base class for every failure originating in a Source implementation."""


class SourceNotFoundError(SourceError):
    """The cloud object no longer exists (permanently deleted or moved)."""


class SourcePermissionError(SourceError):
    """The account lacks permission for the requested operation."""


class SourceRateLimitError(SourceError):
    """The provider throttled us past the retry cap."""


class SourceAuthError(SourceError):
    """The stored token was rejected (401) — interactive re-auth required."""


class SourceDriftError(SourceError):
    """Raised when the file's current cloud-side etag/modifiedTime differs
    from the ``record.etag`` captured at scan time.

    v0.2 sub-phase 5c: the mover calls :meth:`Source.check_drift` immediately
    before :meth:`Source.move_to_trash`.  A drift aborts the entire apply run
    — same semantics as local (size, mtime) drift.  Local sources typically
    raise :class:`duplicate_cleaner.apply.mover.PathChangedError` from the
    verify path; cloud sources raise ``SourceDriftError`` from their
    ``check_drift`` implementation.
    """


@dataclass(frozen=True)
class SourceMetadata:
    """Snapshot of cloud-side metadata pulled during list_files()."""

    etag: str | None = None
    cloud_file_id: str | None = None
    owner: str | None = None
    is_shared: bool = False
    parent_path: str | None = None


@dataclass(frozen=True)
class TrashedLocation:
    """Enough information to restore a previously-trashed file."""

    source_id: str
    original_path: str
    cloud_file_id: str | None = None
    cloud_trash_id: str | None = None
    local_trashed_at_path: Path | None = None


@runtime_checkable
class Source(Protocol):
    """Every file-access backend implements this protocol.

    ``is_read_only_scan`` is a defense-in-depth tripwire: while True,
    ``move_to_trash`` MUST raise so a bug in ``dc scan`` cannot trash a file
    by mistake.  ``apply/mover.py`` constructs sources with the flag off.
    """

    id: str
    is_read_only_scan: bool

    def list_files(self) -> Iterator[FileRecord]:
        ...

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        ...

    def move_to_trash(self, record: FileRecord) -> TrashedLocation:
        ...

    def restore_from_trash(self, loc: TrashedLocation) -> None:
        ...

    def get_metadata(self, record: FileRecord) -> SourceMetadata:
        ...

    def check_drift(self, record: FileRecord) -> None:
        """Verify ``record`` still matches provider-side state.

        v0.2 sub-phase 5c: called by the mover immediately BEFORE
        :meth:`move_to_trash` so a stale scan cannot silently trash the wrong
        file.  For cloud sources compare the re-fetched
        etag/modifiedTime against ``record.etag``; for local sources compare
        size + mtime.  Raises :class:`SourceDriftError` (cloud) or
        ``PathChangedError`` (local) on mismatch — the mover aborts the whole
        run on either.  Return ``None`` when the record is still current.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement check_drift"
        )
