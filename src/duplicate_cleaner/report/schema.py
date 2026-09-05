"""Report data model — Pydantic schemas rendered to both HTML and JSON."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


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


class ReportGroup(BaseModel):
    id: str
    kind: Literal["exact"] = "exact"
    size: int
    hash: str
    reclaim_bytes: int
    members: list[ReportMember]


class Report(BaseModel):
    version: str = "0.1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    roots: list[Path]
    total_files_scanned: int
    total_groups: int
    total_reclaim_bytes: int
    groups: list[ReportGroup]
