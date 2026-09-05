"""Pluggable file-access backends — local filesystem now, cloud in later sub-phases."""
from __future__ import annotations

from duplicate_cleaner.sources.base import (
    ForeignHash,
    Source,
    SourceAuthError,
    SourceDriftError,
    SourceError,
    SourceMetadata,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
    TrashedLocation,
)
from duplicate_cleaner.sources.gdrive import GoogleDriveSource
from duplicate_cleaner.sources.local import LocalFileSystemSource
from duplicate_cleaner.sources.onedrive import OneDriveSource

__all__ = [
    "ForeignHash",
    "GoogleDriveSource",
    "LocalFileSystemSource",
    "OneDriveSource",
    "Source",
    "SourceAuthError",
    "SourceDriftError",
    "SourceError",
    "SourceMetadata",
    "SourceNotFoundError",
    "SourcePermissionError",
    "SourceRateLimitError",
    "TrashedLocation",
]
