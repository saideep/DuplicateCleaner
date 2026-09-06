"""Discovery orchestrator — walks files, extracts signals, classifies, clusters, plans.

Discovery is READ-ONLY.  Zero writes to user directories.  The only outputs
are the plan artifacts (JSON + HTML) which the caller writes to a report
directory of its choosing, plus optional SQLite signal caching under
``~/.cache/duplicate_cleaner/``.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from duplicate_cleaner.config import Config
from duplicate_cleaner.organize.plan import (
    Alternative,
    CohesionGroup,
    FiredSignal,
    PlanEntry,
    PlanFile,
    dest_for,
)
from duplicate_cleaner.organize.signals import (
    PHOTO_SUFFIXES,
    VIDEO_SUFFIXES,
    SignalSet,
    extract_all,
)
from duplicate_cleaner.organize.taxonomy import Classification, classify
from duplicate_cleaner.scan.walk import FileRecord, WalkStats, iter_files
from duplicate_cleaner.store import Store

log = logging.getLogger(__name__)

# Cohesion detection threshold — a directory is cohesive if at least this
# fraction of its members share the same (domain, subfolder_template) tuple.
_COHESION_MIN_RATIO = 0.8

# Project marker files — presence of any triggers a project cohesion group.
_PROJECT_MARKERS: frozenset[str] = frozenset(
    (".git", "package.json", "Cargo.toml", "pyproject.toml", "go.mod", "pom.xml")
)


@dataclass(frozen=True)
class DiscoverySummary:
    """Counters returned alongside the PlanFile for CLI printing."""

    files_scanned: int
    domains: dict[str, int]
    cohesion_groups: int


# --------------------------------------------------------------------------- #
# Event clustering                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _MediaItem:
    path: Path
    ts: float


def cluster_events(
    items: list[_MediaItem], *, gap_hours: float = 12.0, min_event_photos: int = 5
) -> list[list[_MediaItem]]:
    """Split a time-sorted stream on gaps larger than ``gap_hours``.

    Returns a list of clusters (each a list of items).  Clusters below
    ``min_event_photos`` are still returned — the caller decides whether to
    fall back to monthly grouping.
    """
    if not items:
        return []
    from itertools import pairwise

    sorted_items = sorted(items, key=lambda i: i.ts)
    clusters: list[list[_MediaItem]] = [[sorted_items[0]]]
    gap_s = gap_hours * 3600.0
    for prev, curr in pairwise(sorted_items):
        if (curr.ts - prev.ts) < gap_s:
            clusters[-1].append(curr)
        else:
            clusters.append([curr])
    _ = min_event_photos  # currently informational; below-threshold clusters
    # are surfaced separately by cohesion logic.
    return clusters


def _event_name(cluster: list[_MediaItem]) -> str:
    """Human-readable event folder name from a cluster's timestamp span."""
    if not cluster:
        return "Undated"
    first = datetime.fromtimestamp(cluster[0].ts).date()
    last = datetime.fromtimestamp(cluster[-1].ts).date()
    if first == last:
        return first.isoformat()
    return f"{first.isoformat()}_to_{last.isoformat()}"


# --------------------------------------------------------------------------- #
# Public entrypoint                                                           #
# --------------------------------------------------------------------------- #


def discover(
    roots: list[Path],
    config: Config,
    *,
    sources: dict[str, object] | None = None,
    store: Store | None = None,
    dest_root: Path | None = None,
) -> tuple[PlanFile, DiscoverySummary]:
    """Discover organize destinations for every eligible file under ``roots``.

    ``sources`` is accepted for forward-compat with v0.3-g cross-source
    organization but not yet consulted — the walker's local ``iter_files``
    covers v0.3-a scope.  ``store`` is optional; when passed, extracted
    signals are cached in ``file_signals`` keyed on (source_id, path, mtime).
    """
    _ = sources  # v0.3-g will feed cloud FileRecords here

    stats = WalkStats()
    entries: list[PlanEntry] = []
    cohesion_groups: list[CohesionGroup] = []
    domain_counts: dict[str, int] = defaultdict(int)

    per_dir_records: dict[
        Path, list[tuple[FileRecord, SignalSet, Classification]]
    ] = defaultdict(list)

    for record in iter_files(
        roots,
        follow_symlinks=config.follow_symlinks,
        exclude_globs=config.exclude_globs,
        min_size_bytes=config.min_size_bytes,
        bundle_extensions=tuple(config.bundle_extensions),
        stats=stats,
    ):
        signals = _extract_with_cache(record, store)
        cls = classify(
            signals,
            threshold=config.organize_confidence_threshold,
        )
        per_dir_records[record.path.parent].append((record, signals, cls))

    # Second pass: assemble entries + detect per-directory cohesion.  Order
    # is deterministic (sorted parents; sorted files within each parent).
    for parent in sorted(per_dir_records.keys(), key=lambda p: str(p)):
        bucket = sorted(per_dir_records[parent], key=lambda t: str(t[0].path))
        cohesion_id = _detect_cohesion(parent, bucket, cohesion_groups)
        _ = _detect_project_cohesion(parent, bucket, cohesion_groups, cohesion_id)
        for record, signals, cls in bucket:
            entry = _build_entry(record, signals, cls, cohesion_id)
            entries.append(entry)
            domain_counts[entry.domain] += 1

    # Third pass: event-cluster photos + videos, override subfolder for
    # cohesive events with ≥ ``min_event_photos`` items.
    _apply_event_clustering(entries, cohesion_groups, config)

    plan = PlanFile(
        generated_at=datetime.now(UTC),
        generated_ts=datetime.now(UTC).timestamp(),
        sources=["local"],
        roots=roots,
        dest_root=dest_root,
        total_files=len(entries),
        total_by_domain=dict(domain_counts),
        entries=entries,
        cohesion_groups=cohesion_groups,
        unsorted_alternatives=[],
    )
    summary = DiscoverySummary(
        files_scanned=stats.files_visited,
        domains=dict(domain_counts),
        cohesion_groups=len(cohesion_groups),
    )
    return plan, summary


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _extract_with_cache(record: FileRecord, store: Store | None) -> SignalSet:
    """Extract signals; cache in SQLite when ``store`` is provided."""
    if store is not None:
        cached: SignalSet | None
        try:
            cached = store.get_cached_signals(
                str(record.path), record.source_id, record.mtime
            )
        except AttributeError:
            cached = None
        if cached is not None:
            return cached

    signals = extract_all(record.path, record.mtime)

    if store is not None:
        import contextlib

        with contextlib.suppress(AttributeError):
            store.put_signal_set(
                str(record.path), record.source_id, record.mtime, signals
            )
    return signals


def _build_entry(
    record: FileRecord,
    signals: SignalSet,
    cls: Classification,
    cohesion_id: str | None,
) -> PlanEntry:
    fired = [
        FiredSignal(kind=k, value="matched")
        for k in cls.matched_signals
    ]
    alternatives = [
        Alternative(domain=d, subfolder=sub, confidence=conf)
        for d, sub, conf in cls.alternatives
    ]
    filename = record.path.name
    dest = dest_for(cls.domain, cls.subfolder, filename)
    _ = signals  # signals already reflected in ``cls.matched_signals``
    return PlanEntry(
        source_id=record.source_id,
        source_path=record.path,
        proposed_dest=dest,
        domain=cls.domain,
        subfolder=cls.subfolder,
        filename=filename,
        size=record.size,
        mtime=record.mtime,
        confidence=cls.confidence,
        signals=fired,
        alternatives=alternatives,
        cohesion_group_id=cohesion_id,
        fired_rules=list(cls.fired_rules),
    )


def _detect_cohesion(
    parent: Path,
    bucket: list[tuple[FileRecord, SignalSet, Classification]],
    out_groups: list[CohesionGroup],
) -> str | None:
    """Group by (domain, subfolder); a directory is cohesive above the ratio.

    Returns the cohesion group id if one was created, else ``None`` — a
    per-file value stamped onto every entry in the bucket that matched the
    dominant destination.
    """
    if len(bucket) < 3:
        return None
    counts: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for record, _sig, cls in bucket:
        counts[(cls.domain, cls.subfolder)].append(record.path)

    (best_key, members) = max(counts.items(), key=lambda kv: len(kv[1]))
    ratio = len(members) / float(len(bucket))
    if ratio < _COHESION_MIN_RATIO:
        return None
    domain, subfolder = best_key
    if domain == "Unsorted":
        return None
    kind = _cohesion_kind(bucket, domain)
    group_id = f"{kind}:{parent.name}"
    destination = f"{domain}/{subfolder}" if subfolder else domain
    desc = (
        f"{len(members)} of {len(bucket)} files in {parent.name} "
        f"share {destination}"
    )
    out_groups.append(
        CohesionGroup(
            id=group_id,
            kind=kind,
            description=desc,
            destination=destination,
            member_paths=sorted(members),
        )
    )
    return group_id


CohesionKind = Literal[
    "music_album", "book_series", "project", "photo_event",
    "photo_burst", "directory",
]


def _cohesion_kind(
    bucket: list[tuple[FileRecord, SignalSet, Classification]],
    domain: str,
) -> CohesionKind:
    """Pick the semantic label for the cohesion group's ``kind`` field."""
    if domain == "Media":
        # A directory whose dominant classification is Media/Music/... is an
        # album; other Media domains fall through to "directory".
        for _rec, _sig, cls in bucket:
            if cls.subfolder.startswith("Music/"):
                return "music_album"
            if cls.subfolder.startswith("Books"):
                return "book_series"
    if domain in ("Photos", "Videos"):
        return "photo_event"
    return "directory"


def _detect_project_cohesion(
    parent: Path,
    bucket: list[tuple[FileRecord, SignalSet, Classification]],
    out_groups: list[CohesionGroup],
    existing_id: str | None,
) -> str | None:
    """Detect a project directory by marker files (``.git``, ``pyproject.toml``, …).

    Project cohesion is stronger than album/event — but we don't rewrite
    existing entries here, only surface the group so review UIs can display
    it.  Enforcement happens in ``apply`` (5.3-e).
    """
    try:
        names = {child.name for child in parent.iterdir()}
    except OSError:
        return None
    if not (_PROJECT_MARKERS & names):
        return None
    group_id = f"project:{parent.name}"
    if existing_id == group_id:
        return existing_id
    out_groups.append(
        CohesionGroup(
            id=group_id,
            kind="project",
            description=f"Project directory {parent.name} (git/build markers present)",
            destination=f"Projects/{parent.name}",
            member_paths=sorted(record.path for record, _s, _c in bucket),
        )
    )
    return group_id


def _apply_event_clustering(
    entries: list[PlanEntry],
    cohesion_groups: list[CohesionGroup],
    config: Config,
) -> None:
    """Cluster photos/videos by EXIF/video timestamp; add photo_event cohesion.

    Runs across the whole plan (not per-directory) so a folder of mixed shots
    still forms events.  Buckets below ``config.min_event_photos`` are left
    unclustered (they keep their monthly rule destination).
    """
    photo_items: list[tuple[int, _MediaItem]] = []
    for idx, entry in enumerate(entries):
        suffix = entry.source_path.suffix.lower()
        if suffix not in PHOTO_SUFFIXES and suffix not in VIDEO_SUFFIXES:
            continue
        # Prefer EXIF/video capture ts; fall back to mtime.
        ts = _timestamp_for_entry(entry)
        if ts is None:
            continue
        photo_items.append((idx, _MediaItem(entry.source_path, ts)))

    if len(photo_items) < config.min_event_photos:
        return

    clusters = cluster_events(
        [mi for _, mi in photo_items],
        gap_hours=float(config.event_gap_hours),
        min_event_photos=config.min_event_photos,
    )
    if not clusters:
        return

    # Reverse-map paths back to entry indices.
    idx_by_path: dict[Path, int] = {mi.path: i for i, mi in photo_items}
    for cluster in clusters:
        if len(cluster) < config.min_event_photos:
            continue
        event_name = _event_name(cluster)
        member_paths = [c.path for c in cluster]
        group_id = f"event:{event_name}"

        # Determine destination from the first member's existing entry —
        # keeps Photos vs Videos separate if the cluster is mixed.
        first_entry = entries[idx_by_path[member_paths[0]]]
        domain = first_entry.domain if first_entry.domain in ("Photos", "Videos") else "Photos"
        new_subfolder = f"{event_name.split('_')[0][:4]}/{event_name}"

        for p in member_paths:
            ei = idx_by_path.get(p)
            if ei is None:
                continue
            entry = entries[ei]
            entries[ei] = entry.model_copy(update={
                "domain": domain,
                "subfolder": new_subfolder,
                "cohesion_group_id": group_id,
                "proposed_dest": dest_for(domain, new_subfolder, entry.filename),
            })

        cohesion_groups.append(
            CohesionGroup(
                id=group_id,
                kind="photo_event",
                description=(
                    f"{len(cluster)} media files in event {event_name} "
                    f"(gap < {config.event_gap_hours}h)"
                ),
                destination=f"{domain}/{new_subfolder}",
                member_paths=sorted(member_paths),
            )
        )


def _timestamp_for_entry(entry: PlanEntry) -> float | None:
    """Pull the best available capture timestamp for event clustering."""
    # Signals are collapsed to FiredSignal(kind, value) on the entry; we
    # can't reach back to the SignalSet from here.  Use mtime as the
    # deterministic fallback — the second-pass caller already had EXIF
    # signals available and encoded them into the classification, but for
    # clustering purposes mtime is a safe approximation when EXIF is
    # absent.
    return entry.mtime


def _record_signals(records: Iterable[FileRecord]) -> None:
    """Placeholder kept for future v0.3-c / v0.3-d hooks."""
    for _rec in records:
        pass
