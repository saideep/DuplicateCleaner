"""Report data model — Pydantic schemas rendered to both HTML and JSON."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# v0.1.1: groups can now describe archive-whole and bundle roll-ups in
# addition to exact-content duplicate sets. The mover keys on this to
# decide what counts as a legal discard target — see ``apply/mover.py``.
#
# v0.4: ``tree`` groups collapse two copies of the same project directory
# into a single entry — the discard member's ``path`` is the *directory*
# root, not any individual file, and the mover sends the whole directory
# to Trash atomically.  See ``compare/tree.py``.
#
# v0.7: ``image-near-dup`` groups cluster images that hash to
# perceptually-close (but not byte-identical) content via pHash Hamming
# distance under a configured threshold.  Discards flow through the same
# per-file ``send2trash`` rail as ``exact`` groups.  See
# ``compare/image.py``.
#
# v0.8: ``audio-near-dup`` and ``video-near-dup`` groups extend the same
# per-file trash rail to Chromaprint-fingerprint audio matches (same song
# re-encoded at different bitrates) and ffmpeg keyframe-pHash video
# matches (same footage in a different container).  See ``compare/audio.py``
# and ``compare/video.py``.
GroupKind = Literal[
    "exact",
    "archive-whole",
    "tree",
    "image-near-dup",
    "audio-near-dup",
    "video-near-dup",
]


class ReportSignal(BaseModel):
    name: str
    contribution: float


class TreeDiffEntry(BaseModel):
    """One file that differs between two project-directory copies.

    v0.4 project-tree aggregation.  Emitted per differing file inside a
    ``kind="tree"`` group so the HTML report can show a per-file diff
    without expanding a directory of thousands of identical members.  The
    list of hashes is per-group-member positional (matches the surrounding
    ``ReportGroup.members`` order) so a caller can identify which side is
    missing / differs.
    """

    relative_path: str
    hashes_per_member: list[str | None]


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
    # v0.2 additions — all defaulted so v0.1.1 reports load cleanly with
    # ``source_id="local"`` and cloud fields cleared.  See design doc
    # ``docs/design/v0.2-subphase5-cloud-path-validation.md`` §3.1.  Nothing
    # in ``apply/`` or ``paths.py`` may treat these fields as authoritative
    # for local paths — the dispatch key is ``source_id``, and only the
    # source-dispatched cloud branch (added in sub-milestone 5b onward) may
    # consult ``cloud_file_id`` / ``etag``.
    source_id: str = "local"
    cloud_file_id: str | None = None
    etag: str | None = None
    owner: str | None = None
    is_shared: bool = False
    # v0.2.1 addition — True when the member's ``hash`` was normalised by
    # the cross-algo reconciliation pass (BLAKE3 downloaded and cached for
    # cloud members whose siblings used a different algorithm).  Purely
    # informational — the mover treats reconciled and non-reconciled members
    # identically.
    reconciled: bool = False


class ImageNearDupSignal(BaseModel):
    """Metadata attached to a ``kind="image-near-dup"`` :class:`ReportGroup`.

    v0.7 image near-duplicate detection.  Carries the worst pairwise Hamming
    distance inside the cluster (higher = looser match, lower = tighter),
    the total bit budget of the underlying pHash (256 for hash_size=16), and
    the min/max file size in bytes across members so a reviewer can spot
    "these five copies range from a 50 KB thumbnail to a 3 MB original".
    """

    max_pairwise_distance: int
    hash_bits: int = 256
    min_size: int
    max_size: int


class AudioNearDupSignal(BaseModel):
    """Metadata attached to a ``kind="audio-near-dup"`` :class:`ReportGroup`.

    v0.8 audio near-duplicate detection.  ``min_similarity`` is the worst
    pairwise Chromaprint similarity inside the cluster (closer to 1.0 =
    tighter match).  ``duration_range`` captures the min/max duration in
    seconds across members so a reviewer can spot a 3:12 song clustered
    with a 30-second sample preview.
    """

    min_similarity: float
    duration_min_seconds: float
    duration_max_seconds: float


class VideoNearDupSignal(BaseModel):
    """Metadata attached to a ``kind="video-near-dup"`` :class:`ReportGroup`.

    v0.8 video near-duplicate detection.  ``min_similarity`` is the worst
    pairwise keyframe-pHash similarity inside the cluster.
    ``duration_range`` captures min/max duration in seconds across members.
    """

    min_similarity: float
    duration_min_seconds: float
    duration_max_seconds: float


class ReportGroup(BaseModel):
    id: str
    kind: GroupKind = "exact"
    size: int
    hash: str
    reclaim_bytes: int
    members: list[ReportMember]
    # v0.4 project-tree aggregation.  Populated only for ``kind="tree"``
    # groups; ``None`` on every ``exact``/``archive-whole`` group so v0.1+
    # reports round-trip unchanged.
    identical_file_count: int | None = None
    tree_diff: list[TreeDiffEntry] | None = None
    similarity_pct: float | None = None
    # v0.7 image near-duplicate metadata.  Populated only for
    # ``kind="image-near-dup"`` groups; ``None`` on every other kind so
    # v0.4+ reports round-trip unchanged.
    image_near_dup: ImageNearDupSignal | None = None
    # v0.8 audio + video near-duplicate metadata.  Populated only for the
    # matching ``kind="audio-near-dup"`` / ``"video-near-dup"`` groups;
    # ``None`` on every other kind so pre-v0.8 reports round-trip cleanly.
    audio_near_dup: AudioNearDupSignal | None = None
    video_near_dup: VideoNearDupSignal | None = None


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


class NotYetHashedBucket(BaseModel):
    """A cross-source size bucket that could not be reconciled this scan.

    v0.2.1: emitted when the cumulative cross-algo download budget
    (``--max-cloud-download-mb``) would be exceeded by reconciling the
    bucket.  The bucket's members exist and are named so the user can
    lift the cap and re-run, but no cross-source duplicate claim is
    surfaced for them until reconciliation actually runs.
    """

    paths: list[str]
    size: int
    reason: str = "budget_exceeded"


class Report(BaseModel):
    # v0.2 sub-phase 5a: bumped from ``"0.1.1"`` → ``"0.2.0"``.  The loader
    # (``apply/mover.py::load_report``) accepts any older value and treats
    # missing / ``0.1.*`` markers as v0.1.1, applying default field values
    # silently — a v0.1.1 report.json in a checked-out runs directory keeps
    # working under v0.2.  See design doc §3.
    version: str = "0.2.0"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    roots: list[Path]
    total_files_scanned: int
    total_groups: int
    total_reclaim_bytes: int
    groups: list[ReportGroup]
    singletons: list[SingletonEntry] = Field(default_factory=list)
    archive_skips: list[ArchiveSkipEntry] = Field(default_factory=list)
    # v0.2.1 addition — cross-source size buckets skipped because the
    # cumulative cloud-download budget would have been exceeded.  Empty for
    # local-only scans and for scans whose reconciliation stayed under the
    # cap.
    not_yet_hashed_buckets: list[NotYetHashedBucket] = Field(default_factory=list)
    # v0.7: perceptual-hash image near-duplicate groups.  Flow through the
    # same per-file trash rail as ``exact`` groups but live in a dedicated
    # list so consumers (mover, HTML renderer) can dispatch on the kind
    # without walking every ``groups`` entry.  Empty list keeps pre-v0.7
    # reports round-trip clean.
    image_near_dup_groups: list[ReportGroup] = Field(default_factory=list)
    # v0.8: audio near-duplicate groups (Chromaprint fingerprint matches)
    # and video near-duplicate groups (ffmpeg keyframe pHash matches).
    # Same per-file trash rail as ``exact`` groups; dedicated lists so
    # consumers dispatch by family without walking every ``groups`` entry.
    audio_near_dup_groups: list[ReportGroup] = Field(default_factory=list)
    video_near_dup_groups: list[ReportGroup] = Field(default_factory=list)
    # ``discover`` mode: no keeper is proposed on any group. ``dc apply``
    # refuses to run a discover-mode report so nothing accidentally deletes.
    discover: bool = False


# ---------------------------------------------------------------------------
# Manifest schema (v0.2 sub-phase 5a) — Pydantic model backing the on-disk
# ``manifest.json`` written by ``apply/mover.py`` and read by ``apply/undo.py``.
#
# The v0.1.1 manifest was a raw ``dict[str, Any]``.  Sub-phase 5a extracts a
# typed model so cloud-side fields (``source_id``, ``cloud_file_id``,
# ``cloud_trash_id``, ``etag``) are declared in exactly one place and
# guaranteed to round-trip.  Undo (``restore_from_manifest``) continues to
# read the JSON via ``json.loads`` + ``dict.get`` so a v0.1.1 manifest with
# no ``manifest_version`` and no ``source_id`` on entries still restores
# byte-identically — the missing fields are treated as their defaults.
# ---------------------------------------------------------------------------


class ManifestEntry(BaseModel):
    """One row in an apply-run manifest.

    v0.1.1 rows carry only ``original_path``, ``size``, ``mtime``, ``hash``,
    ``trashed_at_path``.  v0.2 rows add the trailing cloud fields; the
    defaults keep old JSON loadable via ``ManifestEntry.model_validate(row)``
    without a forced re-scan.
    """

    original_path: str
    size: int
    mtime: float
    # v0.1.0 manifests (pre-G4) did not stamp ``hash`` — undo falls back
    # to a basename+size scan of the Trash and refuses on ambiguity.
    # Default here preserves that backward-compat load path while still
    # letting Pydantic type-check the field when present.
    hash: str = ""
    trashed_at_path: str | None = None
    # v0.2 additions — see design doc §3.2.
    source_id: str = "local"
    cloud_file_id: str | None = None
    cloud_trash_id: str | None = None
    etag: str | None = None
    # v0.4 project-tree aggregation.  When ``True``, ``original_path``
    # points to a DIRECTORY (a project root), not a file, and undo restores
    # the whole tree from Trash via ``shutil.move``.  ``identical_file_count``
    # + ``project_tree_bytes`` are informational; the mover records them so
    # ``dc undo`` can print an accurate "restored 342 files (2.1 GB)"
    # summary without walking the recovered tree.
    is_project_tree: bool = False
    identical_file_count: int | None = None
    project_tree_bytes: int | None = None


class Manifest(BaseModel):
    """Top-level envelope for ``manifest.json`` on disk.

    ``manifest_version`` is optional on read (missing → treat as v0.1.1 →
    every entry defaults to ``source_id="local"``).  Writers stamp it
    explicitly so downstream tooling can key off the marker.
    """

    manifest_version: str = "0.2.0"
    created_at: str
    roots: list[str] = Field(default_factory=list)
    entries: list[ManifestEntry] = Field(default_factory=list)
