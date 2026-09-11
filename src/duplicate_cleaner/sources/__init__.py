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
    UploadResult,
)
from duplicate_cleaner.sources.gdrive import GoogleDriveSource
from duplicate_cleaner.sources.gphotos import GooglePhotosSource
from duplicate_cleaner.sources.iclouddrive_photos import iCloudPhotosSource
from duplicate_cleaner.sources.local import LocalFileSystemSource
from duplicate_cleaner.sources.onedrive import OneDriveSource

__all__ = [
    "ForeignHash",
    "GoogleDriveSource",
    "GooglePhotosSource",
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
    "UploadResult",
    "iCloudPhotosSource",
]
