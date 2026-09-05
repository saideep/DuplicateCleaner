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
