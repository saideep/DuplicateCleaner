"""Pluggable file-access backends — local filesystem now, cloud in later sub-phases."""
from __future__ import annotations

from duplicate_cleaner.sources.base import (
    ForeignHash,
    Source,
    SourceError,
    SourceMetadata,
    TrashedLocation,
)
from duplicate_cleaner.sources.gdrive import GoogleDriveSource
from duplicate_cleaner.sources.local import LocalFileSystemSource

__all__ = [
    "ForeignHash",
    "GoogleDriveSource",
    "LocalFileSystemSource",
    "Source",
    "SourceError",
    "SourceMetadata",
    "TrashedLocation",
]
