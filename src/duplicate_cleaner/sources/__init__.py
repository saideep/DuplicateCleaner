"""Pluggable file-access backends — local filesystem now, cloud in later sub-phases."""
from __future__ import annotations

from duplicate_cleaner.sources.base import (
    ForeignHash,
    Source,
    SourceError,
    SourceMetadata,
    TrashedLocation,
)
from duplicate_cleaner.sources.local import LocalFileSystemSource

__all__ = [
    "ForeignHash",
    "LocalFileSystemSource",
    "Source",
    "SourceError",
    "SourceMetadata",
    "TrashedLocation",
]
