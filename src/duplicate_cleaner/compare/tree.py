"""Project-tree aggregation — collapse duplicate project directories.

v0.4 milestone.  When two copies of the same project (git repo, npm
package, Cargo crate, ...) surface as thousands of individual exact-dup
matches, this module rolls them up to a single ``kind="tree"`` group so
the user sees "these two folders are the same project" instead of
scrolling through ten thousand near-identical file rows.

Detection is two-phase:

1.  ``detect_project_dirs`` groups every hashed file by its parent
    directory and flags any directory containing a well-known project
    marker (``.git``, ``package.json``, ``Cargo.toml``, ...).
2.  ``aggregate_project_duplicates`` computes Jaccard similarity between
    every pair of project directories' file-hash multisets.  Pairs at or
    above the configured threshold become a project-duplicate cluster.

Cohesion invariant: the mover trashes the discard-side project directory
whole (via ``send2trash`` on the directory itself).  No per-file split is
possible — the invariant lives in one place, mirroring the whole-archive
proposal shape.
"""
from __future__ import annotations

import logging
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from duplicate_cleaner.compare.archive import is_virtual_archive_path
from duplicate_cleaner.hash.pipeline import HashedRecord

GitHeadLookup = Callable[[Path], int | None]

log = logging.getLogger(__name__)


# Files whose presence marks the enclosing directory as a "project root".
# A directory containing any one of these is a candidate for tree
# aggregation.  Directory names (``.git``) match on any child entry named
# ``.git``; file names match on any regular file with that name.
_PROJECT_MARKER_FILES: frozenset[str] = frozenset(
    {
        "package.json",
        "Cargo.toml",
        "pom.xml",
        "pyproject.toml",
        "Pipfile",
        "go.mod",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
    }
)

# Directory-name markers.  Any child directory with this name qualifies
# the parent as a project root.
_PROJECT_MARKER_DIRS: frozenset[str] = frozenset({".git"})

# Additional file-suffix markers (``.sln`` — Visual Studio solution).  Any
# child file whose name ends with one of these qualifies the parent as a
# project root.
_PROJECT_MARKER_SUFFIXES: tuple[str, ...] = (".sln",)


@dataclass(frozen=True)
class ProjectInfo:
    """Summary of one detected project directory.

    ``marker`` is the specific marker that qualified the directory (e.g.
    ``".git"`` or ``"package.json"``) — surfaces in the report so the
    reviewer sees WHY the folder was flagged.  ``file_hashes`` maps every
    file's path (relative to the project root) to its BLAKE3 hex hash.
    Directories with a large fraction of unhashed / archive-member files
    are still recorded — the aggregator uses set membership only.
    """

    root: Path
    marker: str
    name: str
    file_hashes: dict[str, str]
    total_bytes: int = 0


@dataclass(frozen=True)
class TreeDiffEntry:
    """One file that differs between two candidate project copies."""

    relative_path: str
    # Positional per (project_a, project_b) — None when the file is
    # missing on that side.
    hashes_per_member: tuple[str | None, ...]


@dataclass
class ProjectDuplicateGroup:
    """Two (or more) project directories detected as duplicate copies.

    Members share ≥ ``threshold`` fraction of their file-hash multisets.
    ``proposed_keeper_index`` is the position within ``members`` the
    scorer should treat as the keeper — the mover sends every other
    member's ROOT DIRECTORY to Trash atomically.
    """

    members: list[Path]
    similarity: float
    identical_files: int
    differing_files: list[TreeDiffEntry] = field(default_factory=list)
    total_bytes: int = 0
    proposed_keeper_index: int = 0
    # Explanation of the keeper choice — surfaces as a per-signal row in
    # the HTML report.
    keeper_reason: str = ""


# When populating ``differing_files`` we cap the list to keep the JSON
# report human-readable.  The full count is preserved via
# ``identical_files`` + directory listing metadata; callers should not
# treat this cap as authoritative.
_DIFFERING_FILES_CAP = 50


def _dir_has_project_marker(parent: Path, child_names: set[str]) -> str | None:
    """Return the marker string that qualifies ``parent`` as a project root.

    ``child_names`` is the set of ENTRIES observed inside ``parent`` (both
    files and subdirectories).  Marker matching is name-only — callers
    have already excluded unwanted trees via the standard walker.
    """
    for name in child_names:
        if name in _PROJECT_MARKER_DIRS:
            return name
        if name in _PROJECT_MARKER_FILES:
            return name
        for suffix in _PROJECT_MARKER_SUFFIXES:
            if name.endswith(suffix):
                return name
    return None


def detect_project_dirs(
    records: Iterable[HashedRecord],
) -> dict[Path, ProjectInfo]:
    """Return ``{project_root: ProjectInfo}`` for every detected project.

    Groups the records by parent directory, and for each directory checks
    whether its child set contains a known project marker.  Virtual
    archive-member paths (``foo.zip::inner``) are skipped — the archive
    lives at the depth ``outer.zip`` sits at, and any project-root
    detection based on its contents would be spurious.

    Cloud records (``source_id != "local"``) are skipped: cloud paths use
    a synthetic ``<source>://`` scheme that Path parses unpredictably; the
    v0.4 aggregator is local-only.  Cross-source project-tree matching is
    a future milestone.
    """
    by_parent: dict[Path, list[HashedRecord]] = defaultdict(list)
    for rec in records:
        if rec.is_archive_member:
            continue
        if rec.source_id != "local":
            continue
        if is_virtual_archive_path(str(rec.path)):
            continue
        parent = rec.path.parent
        by_parent[parent].append(rec)

    # Now walk UP each parent to see if any ancestor directory (up to the
    # deepest ancestor) qualifies as a project marker.  We attribute at the
    # marker's directory itself, and roll every descendant file into the
    # marker's project — a git repo with subdirectories should be one
    # project, not one project per subdirectory.
    #
    # Strategy: for every parent in ``by_parent`` walk up until we find a
    # directory whose child set matches a marker; call that ``root``.
    # Then union every record whose path is under ``root`` into ``root``'s
    # bucket.
    #
    # Correctness note: we cache the per-directory marker check
    # (``self_marker_cache``) so a repeated iterdir on the same
    # directory (many files share the same parent) is avoided.  We do NOT
    # cache the "no root anywhere upward" answer because it would be
    # unsafe: if directory A has no marker but its ancestor B does, then a
    # later walk starting at A' (a sibling of A, same parent) would
    # short-circuit on A's negative cache and miss the ancestor.  The
    # per-directory cache is enough — directory depth is bounded (~20
    # hops on a real filesystem).
    self_marker_cache: dict[Path, str | None] = {}

    def _self_marker(directory: Path) -> str | None:
        if directory in self_marker_cache:
            return self_marker_cache[directory]
        child_names: set[str] = set()
        try:
            for entry in directory.iterdir():
                child_names.add(entry.name)
        except OSError:
            self_marker_cache[directory] = None
            return None
        marker = _dir_has_project_marker(directory, child_names)
        self_marker_cache[directory] = marker
        return marker

    def _find_root(parent: Path) -> tuple[Path, str] | None:
        """Return the innermost ancestor of ``parent`` (or ``parent`` itself) with a marker."""
        cur: Path = parent
        while True:
            marker = _self_marker(cur)
            if marker is not None:
                return (cur, marker)
            parent_of = cur.parent
            if parent_of == cur:
                return None
            cur = parent_of

    aggregated: dict[Path, dict[str, str]] = defaultdict(dict)
    marker_for: dict[Path, str] = {}
    sizes: dict[Path, int] = defaultdict(int)
    for parent, recs in by_parent.items():
        root_info = _find_root(parent)
        if root_info is None:
            continue
        root, marker = root_info
        marker_for[root] = marker
        for rec in recs:
            try:
                rel = str(rec.path.relative_to(root))
            except ValueError:
                # Shouldn't happen — the marker walk guarantees rec.path
                # sits inside root — but a symlink shenanigan could still
                # bite us.  Skip defensively.
                continue
            aggregated[root][rel] = rec.full_hash
            sizes[root] += int(rec.size)

    out: dict[Path, ProjectInfo] = {}
    for root, files in aggregated.items():
        out[root] = ProjectInfo(
            root=root,
            marker=marker_for[root],
            name=root.name,
            file_hashes=dict(files),
            total_bytes=sizes[root],
        )
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    """Multiset-independent Jaccard over the two hash sets.

    File-hash *sets* (not multisets) are the right granularity — a
    project with 10 copies of ``LICENSE`` (unlikely but possible) should
    not be flagged as more similar to another project than one with a
    single ``LICENSE`` because both share the same license bytes.
    """
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _git_head_ct(root: Path) -> int | None:
    """Return committer timestamp of HEAD, or None if not a git repo / no head."""
    git_dir = root / ".git"
    if not git_dir.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%ct", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def is_git_repo_dirty(root: Path) -> bool:
    """True if ``root`` has a ``.git`` marker and cannot be proven clean.

    A dirty tree is a hard-safety refusal for the mover: uncommitted
    changes represent user work that has no other on-disk copy.  Even if
    every tracked file is identical to another project's tracked files,
    the working-tree diff has not been shipped anywhere; trashing the
    directory would permanently destroy that work.

    K5 (audit pass 14 deferrable): fail closed on every ``git status``
    outcome that is not an explicit clean signal.  Only exit 0 with an
    empty stdout is treated as clean; a corrupted ``.git`` from an
    interrupted rsync (typically exit 128), any other non-zero exit, a
    subprocess timeout, or a missing ``git`` binary all resolve to
    "dirty" — safer to refuse the discard than to trash a working tree
    we could not verify.  A directory without a ``.git`` marker is never
    checked at all (non-git projects flow through their own path).
    """
    git_dir = root / ".git"
    if not git_dir.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Missing git binary, permission failures, timeouts: cannot verify
        # → treat as dirty.  Better to refuse the proposal than to trash
        # user work under an unknown filesystem state.
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _score_keeper(
    members: list[Path],
    git_head_lookup: GitHeadLookup | None = None,
) -> tuple[int, str]:
    """Pick the ``proposed_keeper_index`` and a one-line rationale.

    Layered rules:
    1. If more than one member has a ``.git`` directory, the one with the
       newest HEAD commit wins.
    2. Otherwise, members inside a folder whose name looks like a backup
       marker (``backup``, ``old``, ``archive``, ``bak``) lose the keeper
       role.  A member NOT inside such a folder wins.
    3. Final tie-break: shallowest path (fewest ``.parts``); then
       lexicographic path string for determinism.
    """
    _BACKUP_TOKENS = frozenset(
        {"backup", "backups", "old", "older", "archive", "archives", "bak", "copy", "copies"}
    )

    def _has_backup_ancestor(p: Path) -> bool:
        return any(part.lower() in _BACKUP_TOKENS for part in p.parts)

    lookup = git_head_lookup if git_head_lookup is not None else _git_head_ct
    head_ts: list[int | None] = [lookup(m) for m in members]
    both_git = sum(1 for t in head_ts if t is not None)
    if both_git >= 2:
        best_idx = -1
        best_ts = -1
        for i, ts in enumerate(head_ts):
            if ts is None:
                continue
            if ts > best_ts:
                best_ts = ts
                best_idx = i
        if best_idx >= 0:
            return best_idx, f"newer git HEAD ({best_ts})"

    non_backup = [i for i, m in enumerate(members) if not _has_backup_ancestor(m)]
    if non_backup and len(non_backup) < len(members):
        # Some members are under a backup marker, some are not — the
        # non-backup member wins.  If multiple qualify, fall through to
        # the shallowest-path tie-break inside that subset.
        candidates = non_backup
        best = min(
            candidates,
            key=lambda i: (len(members[i].parts), str(members[i])),
        )
        return best, "not under a backup-marker folder"

    best = min(range(len(members)), key=lambda i: (len(members[i].parts), str(members[i])))
    return best, "shallowest path (tie-break)"


def aggregate_project_duplicates(
    projects: dict[Path, ProjectInfo],
    threshold: float = 0.90,
    *,
    git_head_lookup: GitHeadLookup | None = None,
) -> list[ProjectDuplicateGroup]:
    """Return one :class:`ProjectDuplicateGroup` per connected duplicate cluster.

    v0.4 (K2 fix): pairwise Jaccard over file-hash sets, unioned via
    union-find so N similar projects become ONE group of size N — not
    N*(N-1)/2 pair groups.  Previously the mover trashed a shared discard
    on the first pass and aborted on subsequent pair groups with
    "disappeared between validate and move"; reclaim also over-counted by
    pair-count.

    Two projects belong to the same component when their pairwise Jaccard
    similarity is at or above ``threshold``.  Transitivity is honoured:
    A~B and B~C at threshold pull A, B, C into one component even when
    A~C is below threshold, matching the "same repo copied around" case.
    The emitted group's ``similarity`` field is the MIN pairwise
    similarity within the component (worst-case), so a downstream reader
    can still gauge cluster tightness.
    """
    if not projects:
        return []
    roots = list(projects.keys())
    n = len(roots)
    hash_sets: dict[Path, set[str]] = {
        r: set(projects[r].file_hashes.values()) for r in roots
    }

    # Union-find over root indices.  Path-compressed find + union by size.
    parent: list[int] = list(range(n))
    size: list[int] = [1] * n

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        if size[ra] < size[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        size[ra] += size[rb]

    # Track every pairwise similarity inside each eventual component so we
    # can report the worst-case Jaccard on the emitted group.
    pair_sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = _jaccard(hash_sets[roots[i]], hash_sets[roots[j]])
            if sim < threshold:
                continue
            pair_sims[(i, j)] = sim
            _union(i, j)

    # Group root indices by their component representative.
    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[_find(i)].append(i)

    out: list[ProjectDuplicateGroup] = []
    for members_idx in components.values():
        if len(members_idx) < 2:
            continue
        members_idx.sort()
        member_paths = [roots[i] for i in members_idx]
        idx_set = set(members_idx)
        component_sims = [
            s for (a, b), s in pair_sims.items() if a in idx_set and b in idx_set
        ]
        # Worst-case similarity gives the honest floor for the cluster.
        min_sim = min(component_sims) if component_sims else 1.0
        out.append(
            _build_group(
                member_paths,
                projects,
                min_sim,
                git_head_lookup=git_head_lookup,
            )
        )
    return out


def _build_group(
    members: list[Path],
    projects: dict[Path, ProjectInfo],
    similarity: float,
    *,
    git_head_lookup: GitHeadLookup | None = None,
) -> ProjectDuplicateGroup:
    """Assemble a ProjectDuplicateGroup + compute keeper + diff."""
    # Compute intersection / difference over relative paths.
    per_member_files: list[dict[str, str]] = [
        dict(projects[m].file_hashes) for m in members
    ]
    all_rels: set[str] = set()
    for pf in per_member_files:
        all_rels.update(pf.keys())

    identical = 0
    differing: list[TreeDiffEntry] = []
    for rel in sorted(all_rels):
        hashes: list[str | None] = [pf.get(rel) for pf in per_member_files]
        first = hashes[0]
        if first is not None and all(h == first for h in hashes):
            identical += 1
            continue
        if len(differing) < _DIFFERING_FILES_CAP:
            differing.append(
                TreeDiffEntry(
                    relative_path=rel,
                    hashes_per_member=tuple(hashes),
                )
            )
        # Anything past the cap is still counted implicitly via
        # (len(all_rels) - identical - len(differing)); the caller can
        # display "N more differing files" from that arithmetic.

    keeper_idx, reason = _score_keeper(members, git_head_lookup=git_head_lookup)

    # K2: total_bytes = the reclaim if EVERY non-keeper member is trashed.
    # For an N-way component this is the sum of N-1 sizes; the old pairwise
    # emitter would have double-counted each non-keeper across pair groups.
    total_bytes = sum(
        projects[members[i]].total_bytes
        for i in range(len(members))
        if i != keeper_idx
    )

    return ProjectDuplicateGroup(
        members=list(members),
        similarity=similarity,
        identical_files=identical,
        differing_files=differing,
        total_bytes=total_bytes,
        proposed_keeper_index=keeper_idx,
        keeper_reason=reason,
    )


def project_root_contains(project_root: Path, file_path: Path) -> bool:
    """True if ``file_path`` sits inside ``project_root``.

    Uses purely-lexical containment on the string form so callers can pass
    already-resolved paths without a second stat.
    """
    try:
        file_path.relative_to(project_root)
        return True
    except ValueError:
        return False
