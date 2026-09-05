"""Report data model — Pydantic schemas rendered to both HTML and JSON."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# v0.1.1: groups can now describe archive-whole and bundle roll-ups in
# addition to exact-content duplicate sets. The mover keys on this to
# decide what counts as a legal discard target — see ``apply/mover.py``.
GroupKind = Literal["exact", "archive-whole"]


class ReportSignal(BaseModel):
    name: str
    contribution: float


class ReportMember(BaseModel):
    path: Path
    size: int
    mtime: float
    hash: str
    score: float
    signals: list[ReportSignal] = Field(default_factory=list)
    is_proposed_keeper: bool = False
    is_informational: bool = False
    is_archive_member: bool = False
    is_bundle: bool = False


class ReportGroup(BaseModel):
    id: str
    kind: GroupKind = "exact"
    size: int
    hash: str
    reclaim_bytes: int
    members: list[ReportMember]


class ArchiveSkipEntry(BaseModel):
    """One archive DuplicateCleaner refused to descend into (or could not)."""

    path: str
    reason: str
    error: str | None = None


class SingletonEntry(BaseModel):
    """One file with exactly one occurrence in the whole scan.

    Never a discard candidate. Surfaced in the report because a scan of an
    unfamiliar drive doubles as a "what have I got that exists nowhere else"
    inventory.
    """

    path: Path
    size: int
    mtime: float
    hash: str


class Report(BaseModel):
    version: str = "0.1.1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    roots: list[Path]
    total_files_scanned: int
    total_groups: int
    total_reclaim_bytes: int
    groups: list[ReportGroup]
    singletons: list[SingletonEntry] = Field(default_factory=list)
    archive_skips: list[ArchiveSkipEntry] = Field(default_factory=list)
    # ``discover`` mode: no keeper is proposed on any group. ``dc apply``
    # refuses to run a discover-mode report so nothing accidentally deletes.
    discover: bool = False
