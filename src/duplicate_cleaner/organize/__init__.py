"""Organizer package — read-plan-review-apply file organization (v0.3)."""
from __future__ import annotations

from duplicate_cleaner.organize.discover import DiscoverySummary, discover
from duplicate_cleaner.organize.plan import (
    TAXONOMY_VERSION,
    Alternative,
    CohesionGroup,
    FiredSignal,
    PlanEntry,
    PlanFile,
)
from duplicate_cleaner.organize.signals import (
    FilenameSignalExtractor,
    MusicSignalExtractor,
    PDFSignalExtractor,
    PhotoSignalExtractor,
    SignalExtractor,
    SignalSet,
    VideoSignalExtractor,
    extract_all,
)
from duplicate_cleaner.organize.taxonomy import (
    Classification,
    TaxonomyRule,
    classify,
    default_rules,
)

__all__ = [
    "TAXONOMY_VERSION",
    "Alternative",
    "Classification",
    "CohesionGroup",
    "DiscoverySummary",
    "FilenameSignalExtractor",
    "FiredSignal",
    "MusicSignalExtractor",
    "PDFSignalExtractor",
    "PhotoSignalExtractor",
    "PlanEntry",
    "PlanFile",
    "SignalExtractor",
    "SignalSet",
    "TaxonomyRule",
    "VideoSignalExtractor",
    "classify",
    "default_rules",
    "discover",
    "extract_all",
]
