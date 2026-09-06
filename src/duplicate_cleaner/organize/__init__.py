"""Organizer package — read-plan-review-apply file organization (v0.3)."""
from __future__ import annotations

from duplicate_cleaner.organize.discover import DiscoverySummary, discover
from duplicate_cleaner.organize.mover import (
    ApplyPlanResult,
    OrganizeApplyError,
    OrganizeDriftError,
    apply_plan,
)
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
from duplicate_cleaner.organize.undo import (
    OrganizeUndoError,
    RestoreOrganizeResult,
    restore_from_organize_manifest,
)

__all__ = [
    "TAXONOMY_VERSION",
    "Alternative",
    "ApplyPlanResult",
    "Classification",
    "CohesionGroup",
    "DiscoverySummary",
    "FilenameSignalExtractor",
    "FiredSignal",
    "MusicSignalExtractor",
    "OrganizeApplyError",
    "OrganizeDriftError",
    "OrganizeUndoError",
    "PDFSignalExtractor",
    "PhotoSignalExtractor",
    "PlanEntry",
    "PlanFile",
    "RestoreOrganizeResult",
    "SignalExtractor",
    "SignalSet",
    "TaxonomyRule",
    "VideoSignalExtractor",
    "apply_plan",
    "classify",
    "default_rules",
    "discover",
    "extract_all",
    "restore_from_organize_manifest",
]
