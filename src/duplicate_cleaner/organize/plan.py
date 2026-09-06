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
    """One file's proposed destination.

    ``capture_ts`` is the best-available capture timestamp (EXIF
    DateTimeOriginal or video creation date) in seconds since epoch, per
    design § 4.  ``None`` when no metadata was extracted — the event-
    clustering pass in :mod:`organize.discover` then falls back to
    ``mtime``.  Wired up in v0.3-c (audit pass 13 finding: the previous
    ``_timestamp_for_entry`` unconditionally returned mtime, defeating
    EXIF-based event boundaries).
    """

    source_id: str = "local"
    source_path: Path
    proposed_dest: str
    domain: str
    subfolder: str
    filename: str
    size: int
    mtime: float
    capture_ts: float | None = None
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


# --------------------------------------------------------------------------- #
# Organize undo manifest schema (v0.3-c)                                      #
# --------------------------------------------------------------------------- #


class OrganizeManifestEntry(BaseModel):
    """One row of an organize apply manifest.

    Written by :mod:`duplicate_cleaner.organize.mover` after each successful
    move, read by :mod:`duplicate_cleaner.organize.undo` on restore.  Every
    field is required — a hand-edited manifest with a missing or wrong-
    typed column raises a Pydantic ``ValidationError`` at load time (audit
    pass 13 finding: previously routed via ``json.loads`` + ``dict.get``
    which only enforced ``isinstance(source, str)``).
    """

    source_path: Path
    dest_path: Path
    size: int
    mtime: float
    hash: str | None = None
    capture_ts: float | None = None
    cohesion_group_id: str | None = None
    cross_volume: bool = False
    ts: float


class OrganizeManifest(BaseModel):
    """Top-level envelope for an organize apply manifest.

    v0.3-c: schema-locked so a hand-edited manifest cannot silently
    degrade to per-field ``dict.get`` behaviour on undo.  ``dest_root``
    is required — a manifest with a missing dest_root cannot bound the
    empty-parent-cleanup walker to a safe subtree (audit pass 13
    finding).  ``manifest_version`` is the modern spelling; the legacy
    key ``"version"`` remains recognised by :meth:`load_permissive` for
    forward compatibility.
    """

    manifest_version: Literal["0.3.0"] = "0.3.0"
    kind: Literal["organize"] = "organize"
    created_at: str
    dest_root: Path
    roots: list[str] = Field(default_factory=list)
    entries: list[OrganizeManifestEntry] = Field(default_factory=list)
    collisions: list[dict[str, str]] = Field(default_factory=list)
    plan_path: str | None = None
    run_ts: float | None = None
