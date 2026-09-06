"""Rule-based scorer — picks the "right home" for each duplicate group."""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from duplicate_cleaner.compare.exact import Group
from duplicate_cleaner.config import Config

_MARKER_TOKENS: frozenset[str] = frozenset(
    {
        "backup",
        "backups",
        "old",
        "older",
        "archive",
        "archives",
        "duplicate",
        "duplicates",
        "copy",
        "copies",
    }
)

# Tokenizes a path segment into CamelCase / snake_case / kebab-case parts.
_TOKENIZE_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$|[^A-Za-z])|\d+")


def _has_path_marker(path: Path) -> bool:
    for part in path.parts:
        for tok in _TOKENIZE_RE.findall(part):
            if tok.lower() in _MARKER_TOKENS:
                return True
    return False

# Filename patterns: "foo (1).ext", "foo copy.ext", "foo copy 2.ext", "foo-2.ext".
FILENAME_COPY_RE = re.compile(
    r"(?i)("
    r"\s\(\d+\)(?=\.[^.]+$)"
    r"|\scopy(?=\.[^.]+$)"
    r"|\scopy\s\d+(?=\.[^.]+$)"
    r"|-\d+(?=\.[^.]+$)"
    r")"
)

HOMEISH_DIR_NAMES: frozenset[str] = frozenset(
    {"Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music"}
)


@dataclass
class ScoredMember:
    """One member of an exact-duplicate group with its score breakdown.

    v0.2 sub-milestone 5e — carries the source-side fields the scorer
    needs for cross-source signals.  ``source_id`` defaults to ``"local"``
    so every v0.1.1 construction site keeps working; cloud sources stamp
    their own id.  ``is_shared`` is a monotone informational-only marker —
    once True, no scoring rule can make the member a discard candidate.

    v0.3-c — the ``is_singleton_across_sources`` field was removed as
    dead code (audit pass 12 + 13).  No production code path ever set
    it to True; the singleton-never-a-discard invariant is enforced at
    the mover and by ``group_by_hash`` filtering group size < 2.
    """

    path: Path
    size: int
    mtime: float
    inode: int
    dev: int
    nlink: int
    hash: str
    score: float = 0.0
    signals: list[tuple[str, float]] = field(default_factory=list)
    is_proposed_keeper: bool = False
    is_informational: bool = False
    is_archive_member: bool = False
    is_bundle: bool = False
    # v0.2 sub-milestone 5e additions — all defaulted so v0.1.1 tests and
    # construction sites keep passing untouched.
    source_id: str = "local"
    is_shared: bool = False
    # v0.2.1 additions — plumb the cloud identity fields through to the
    # report so the mover can dispatch cross-source discards, and mark
    # members whose hash was normalised by cross-algo reconciliation.
    cloud_file_id: str | None = None
    etag: str | None = None
    owner: str | None = None
    reconciled: bool = False


@dataclass
class ScoredGroup:
    """A group after scoring — includes reclaim bytes if the non-keepers are trashed."""

    id: str
    hash: str
    size: int
    members: list[ScoredMember]
    reclaim_bytes: int


def _is_under(child: Path, parent: Path) -> bool:
    """True if ``child`` sits inside ``parent`` — both resolved to physical form.

    Resolving both sides handles symlinked/firmlinked home paths where
    ``relative_to`` alone would false-negative.
    """
    try:
        child_r = child.resolve()
        parent_r = parent.resolve()
        child_r.relative_to(parent_r)
        return True
    except (ValueError, OSError):
        return False


def _under_any(child: Path, parents: list[Path]) -> bool:
    return any(_is_under(child, p) for p in parents)


def _has_homeish_parent(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name in HOMEISH_DIR_NAMES:
            return parent
    return None


def _is_git_repo_clean(path: Path, cache: dict[Path, bool]) -> bool:
    """Return True if ``path`` is inside a clean git working tree.

    Runs at most one ``git status`` per repo root within a scoring pass —
    the shared ``cache`` memoizes the result per repo root.
    """
    try:
        parents = list(path.parents)
    except OSError:
        return False
    for parent in parents:
        if not (parent / ".git").exists():
            continue
        cached = cache.get(parent)
        if cached is not None:
            return cached
        try:
            result = subprocess.run(
                ["git", "-C", str(parent), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            clean = result.returncode == 0 and result.stdout.strip() == ""
        except (OSError, subprocess.SubprocessError):
            clean = False
        cache[parent] = clean
        return clean
    return False


def _is_external_drive(path: Path) -> bool:
    return str(path).startswith("/Volumes/")


def score_group(
    group: Group,
    config: Config,
    weights: dict[str, float],
    git_clean_cache: dict[Path, bool] | None = None,
    clone_id_lookup: Callable[[Path], int | None] | None = None,
) -> list[ScoredMember]:
    """Score every member; mark the highest-scoring non-informational as keeper.

    ``clone_id_lookup`` overrides :func:`sys.apfs.get_clone_id`; tests
    inject their own to simulate APFS clone families without needing a
    real APFS volume.
    """
    members: list[ScoredMember] = [
        ScoredMember(
            path=h.path,
            size=h.size,
            mtime=h.mtime,
            inode=h.inode,
            dev=h.dev,
            nlink=h.nlink,
            hash=h.full_hash,
            is_archive_member=h.is_archive_member,
            is_bundle=h.is_bundle,
            source_id=h.source_id,
            is_shared=h.is_shared,
            cloud_file_id=h.cloud_file_id,
            etag=h.etag,
            owner=h.owner,
            reconciled=h.reconciled,
        )
        for h in group.members
    ]

    # Archive members are always informational — never a keeper, never a
    # discard candidate. The mover-level whole-archive proposal is what
    # actually reclaims space when every member is a duplicate elsewhere.
    for m in members:
        if m.is_archive_member:
            m.is_informational = True

    # v0.2 sub-milestone 5e — cross-source informational-only markers.
    # ``is_shared`` (cloud shared-with-me files) is a monotone
    # informational flag — once set, no other signal can un-set it.
    # Applied at the same layer as archive/hardlink/APFS marking so the
    # scorer never proposes deleting a shared file.  Enforced here for
    # defense-in-depth even though upstream code paths (mover, hash
    # pipeline) also honor the invariant.
    #
    # v0.3-c — the sibling ``is_singleton_across_sources`` field was
    # removed as dead code (audit pass 12 + 13): the invariant was never
    # actually set to True in production, and singleton-safety is now
    # enforced by ``group_by_hash`` filtering group size < 2 plus the
    # mover's own singleton guard.
    for m in members:
        if m.is_shared:
            m.is_informational = True

    # Hard-link detection: two-pass so every member of an inode family is
    # marked informational (not just the second-seen ones). Archive members
    # are excluded — they don't map to a real inode.
    id_counts: dict[tuple[int, int], int] = {}
    for m in members:
        if m.is_archive_member:
            continue
        key = (m.dev, m.inode)
        id_counts[key] = id_counts.get(key, 0) + 1
    for m in members:
        if m.is_archive_member:
            continue
        if id_counts[(m.dev, m.inode)] > 1:
            m.is_informational = True

    # APFS clone lineage detection: two files sharing a clone id share
    # bytes on disk. Two-pass, same shape as the hard-link check, so a
    # whole clone family is marked informational. The lookup is opt-in —
    # callers that don't pass one skip clone detection entirely so tests
    # against non-clone corpora stay deterministic.
    if clone_id_lookup is not None:
        clone_ids: dict[int, list[ScoredMember]] = {}
        for m in members:
            if m.is_archive_member:
                continue
            cid = clone_id_lookup(m.path)
            if cid is None or cid == 0:
                continue
            clone_ids.setdefault(cid, []).append(m)
        for _cid, family in clone_ids.items():
            if len(family) > 1:
                for m in family:
                    m.is_informational = True

    non_info = [m for m in members if not m.is_informational]
    # If the entire group is one hardlink family, propose no keeper — the
    # discard set is empty and reclaim is zero (send2trash on any name
    # would leave the inode alive under the other names).
    if not non_info:
        return members

    newest_mtime = max(m.mtime for m in non_info)
    oldest_mtime = min(m.mtime for m in non_info)
    mtime_varies = newest_mtime != oldest_mtime
    largest_size = max(m.size for m in non_info)
    size_varies = any(m.size < largest_size for m in non_info)
    any_internal = any(not _is_external_drive(m.path) for m in non_info)
    # v0.2 sub-milestone 5e: whether any non-informational member is
    # local drives the ``cloud_when_local_exists`` penalty.  Filtering on
    # ``non_info`` avoids the pathological case where a shared-informational
    # local member would count as "the local peer" and stamp a -3 on every
    # cloud sibling.
    group_has_local = any(m.source_id == "local" for m in non_info)

    cache = git_clean_cache if git_clean_cache is not None else {}
    for m in non_info:
        _score_one(
            m,
            config=config,
            weights=weights,
            newest_mtime=newest_mtime,
            oldest_mtime=oldest_mtime,
            mtime_varies=mtime_varies,
            largest_size=largest_size,
            size_varies=size_varies,
            any_internal=any_internal,
            git_clean_cache=cache,
            group_has_local=group_has_local,
        )

    keeper = max(non_info, key=lambda m: (m.score, -len(m.path.parts), str(m.path)))
    keeper.is_proposed_keeper = True
    return members


def _score_one(
    m: ScoredMember,
    *,
    config: Config,
    weights: dict[str, float],
    newest_mtime: float,
    oldest_mtime: float,
    mtime_varies: bool,
    largest_size: int,
    size_varies: bool,
    any_internal: bool,
    git_clean_cache: dict[Path, bool],
    group_has_local: bool = False,
) -> None:
    # v0.2 sub-milestone 5e: cross-source signals.  Applied FIRST so the
    # cloud/local dispatch key is stamped even for members whose path-based
    # signals (marker/copy/depth) are the same across sources.
    if m.source_id != "local" and group_has_local:
        w = weights.get("cloud_when_local_exists", -3.0)
        m.signals.append(("cloud entry when local copy exists", w))
        m.score += w

    if m.source_id != "local":
        order = config.retained_cloud_order
        if order:
            try:
                idx = order.index(m.source_id)
                # Prefer sources earlier in the list.  Weight is
                # ``len(order) - idx - 1``: first source scores +N-1, last
                # listed source scores 0, and unlisted sources are dropped
                # below every listed source by a small negative penalty.
                w = float(max(0, len(order) - idx - 1))
                m.signals.append(
                    (f"retained_cloud_order[{m.source_id}]", w)
                )
                m.score += w
            except ValueError:
                # Not in the list — deprioritise below every listed source.
                # A tiny negative constant is enough to break the tie without
                # swamping the path-based scorer signals.
                w = -0.01
                m.signals.append(
                    ("retained_cloud_order[unlisted]", w)
                )
                m.score += w

    if _has_path_marker(m.path):
        w = weights["path_marker_backup"]
        m.signals.append(("path marker (backup/old/copy/archive)", w))
        m.score += w

    if FILENAME_COPY_RE.search(m.path.name):
        w = weights["filename_copy_marker"]
        m.signals.append(("filename copy marker", w))
        m.score += w

    under_active = (
        _under_any(m.path, config.active_homes) if config.active_homes else False
    )
    if under_active:
        w = weights["under_active_home"]
        m.signals.append(("under active home", w))
        m.score += w

    homeish = _has_homeish_parent(m.path)
    if homeish is not None and not under_active:
        w = weights["under_inactive_home"]
        m.signals.append(("under archived/backup home", w))
        m.score += w

    if homeish is not None and homeish.name == "Downloads":
        w = weights["downloads_transit"]
        m.signals.append(("inside Downloads (transit zone)", w))
        m.score += w

    depth = len(m.path.parts)
    w = weights["depth_penalty_per_level"] * depth
    m.signals.append((f"depth ({depth} segments)", w))
    m.score += w

    if mtime_varies and m.mtime == newest_mtime:
        w = weights["newest_mtime"]
        m.signals.append(("newest mtime", w))
        m.score += w
    if mtime_varies and m.mtime == oldest_mtime:
        w = weights["oldest_mtime_tiebreak"]
        m.signals.append(("oldest mtime (tie-break)", w))
        m.score += w

    if _is_git_repo_clean(m.path, git_clean_cache):
        w = weights["clean_git_repo"]
        m.signals.append(("inside clean git repo", w))
        m.score += w

    if _is_external_drive(m.path) and any_internal:
        w = weights["external_drive_penalty"]
        m.signals.append(("external drive (internal copy exists)", w))
        m.score += w

    if size_varies and m.size == largest_size:
        w = weights["larger_size"]
        m.signals.append(("larger file", w))
        m.score += w


def score_groups(
    groups: list[Group],
    config: Config,
    weights: dict[str, float],
    *,
    clone_id_lookup: Callable[[Path], int | None] | None = None,
) -> list[ScoredGroup]:
    """Score every group; compute reclaim bytes."""
    scored: list[ScoredGroup] = []
    git_clean_cache: dict[Path, bool] = {}
    for g in groups:
        members = score_group(
            g,
            config,
            weights,
            git_clean_cache=git_clean_cache,
            clone_id_lookup=clone_id_lookup,
        )
        reclaim = sum(
            m.size
            for m in members
            if not m.is_proposed_keeper and not m.is_informational
        )
        scored.append(
            ScoredGroup(
                id=g.hash[:16],
                hash=g.hash,
                size=g.size,
                members=members,
                reclaim_bytes=reclaim,
            )
        )
    return scored
