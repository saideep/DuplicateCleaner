"""Pydantic schemas for the organize plan file (v0.3 discovery output)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

# Taxonomy version marker — bumped whenever the rule catalog changes shape or
# semantics.  Consumers key off this for schema-forward-compat.
TAXONOMY_VERSION = "0.3.0"


class FiredSignal(BaseModel):
    """A single signal that contributed to a classification decision."""

    kind: str
    value: str
    contribution: float = 0.0


class Alternative(BaseModel):
    """Second- or third-best classification for a plan entry."""

    domain: str
    subfolder: str
    confidence: float


class PlanEntry(BaseModel):
    """One file's proposed destination."""

    source_id: str = "local"
    source_path: Path
    proposed_dest: str
    domain: str
    subfolder: str
    filename: str
    size: int
    mtime: float
    confidence: float
    signals: list[FiredSignal] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    cohesion_group_id: str | None = None
    fired_rules: list[str] = Field(default_factory=list)


class CohesionGroup(BaseModel):
    """A set of files that must move together."""

    id: str
    kind: Literal[
        "music_album",
        "book_series",
        "project",
        "photo_event",
        "photo_burst",
        "directory",
    ]
    description: str
    destination: str
    member_paths: list[Path]


class PlanFile(BaseModel):
    """Top-level plan artifact written by ``dc organize discover``.

    ``entries`` and ``cohesion_groups`` are the source of truth; the review UI
    and ``apply`` step re-validate this schema so hand-edits are safe.
    """

    version: Literal["0.3.0"] = "0.3.0"
    taxonomy_version: str = TAXONOMY_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    generated_ts: float = Field(default_factory=lambda: datetime.now(UTC).timestamp())
    sources: list[str] = Field(default_factory=lambda: ["local"])
    roots: list[Path] = Field(default_factory=list)
    dest_root: Path | None = None
    total_files: int = 0
    total_by_domain: dict[str, int] = Field(default_factory=dict)
    entries: list[PlanEntry] = Field(default_factory=list)
    cohesion_groups: list[CohesionGroup] = Field(default_factory=list)
    unsorted_alternatives: list[Alternative] = Field(default_factory=list)


def dest_for(domain: str, subfolder: str, filename: str) -> str:
    """Compose a POSIX-style destination string from parts.

    Kept centralized so every writer produces the same shape.
    """
    parts = [domain]
    if subfolder:
        parts.extend(str(PurePosixPath(subfolder)).split("/"))
    parts.append(filename)
    return str(PurePosixPath(*parts))
