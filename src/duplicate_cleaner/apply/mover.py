"""Move discarded duplicates to Trash — dry-run by default, always writes an undo manifest first."""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from duplicate_cleaner.apply.trash import default_trash_fn
from duplicate_cleaner.auth.accounts import AccountsRegistry
from duplicate_cleaner.compare.archive import (
    ARCHIVE_SEP,
    is_virtual_archive_path,
)
from duplicate_cleaner.compare.tree import is_git_repo_dirty
from duplicate_cleaner.paths import (
    is_within,
    resolve_for_check,
    validate_cloud_entry_with_authorized,
    validate_not_excluded,
    validate_scan_root_candidate,
)
from duplicate_cleaner.report.schema import (
    Manifest,
    ManifestEntry,
    Report,
    ReportGroup,
    ReportMember,
)
from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    Source,
    SourceAuthError,
    SourceDriftError,
    SourceError,
    SourceRateLimitError,
)

log = logging.getLogger(__name__)

RUNS_DIR = Path.home() / ".local" / "share" / "duplicate_cleaner" / "runs"

TrashFn = Callable[[Path], Path | None]


class ApplyError(RuntimeError):
    """Raised when apply cannot safely proceed."""


class PathChangedError(ApplyError):
    """A path in the report no longer matches disk state."""


def load_report(json_path: Path) -> Report:
    """Deserialize a Report from JSON on disk."""
    data = json.loads(json_path.read_text())
    return Report.model_validate(data)


def _iter_all_groups(report: Report) -> list[ReportGroup]:
    """Return every actionable group across every dedicated near-dup list.

    v0.7: image near-duplicate groups live in a dedicated field on the
    Report so consumers can dispatch on the family without walking every
    ``groups`` entry, but the mover treats them identically to
    ``kind="exact"`` groups — same per-file drift check, same
    ``send2trash`` rail, same manifest schema.  v0.8: audio and video
    near-duplicate groups follow the exact same shape and share this
    iterator.  Rolling every list into one keeps the validator + planner
    loops single-source and guarantees a new group family can never slip
    past a check that only ran on ``report.groups``.
    """
    return [
        *report.groups,
        *report.image_near_dup_groups,
        *report.audio_near_dup_groups,
        *report.video_near_dup_groups,
    ]


def _validate_tree_group(
    group: ReportGroup,
    resolved_roots: list[Path],
    resolved_active_homes: list[Path] | None,
) -> None:
    """Enforce project-tree safety rails on one ``kind="tree"`` group.

    v0.4: tree groups discard whole DIRECTORIES.  Each discard member's
    resolved directory MUST:

    * pass ``validate_not_excluded`` (never inside ``/System``, ``~/Library``,
      etc.),
    * sit inside at least one recorded scan root (same as file discards),
    * sit inside at least one ``active_home`` (defense-in-depth — a tree
      discard is much larger than a file discard and the safety envelope
      is proportional).  ``apply_report`` refuses up-front when
      ``active_homes`` is empty/None, so a non-empty resolved list is a
      caller invariant here.  We re-assert it so a future refactor cannot
      quietly weaken the rail.

    Additionally, refuses any discard whose git working tree is dirty
    (``git status --porcelain`` non-empty) — uncommitted work has not
    been shipped anywhere and cannot be reconstructed from another
    directory just because the tracked file set matches.
    """
    # K1 (audit pass 14 ship-blocker): fail closed on the active-home rail.
    # A missing / empty active_homes list is a caller bug, not a licence to
    # skip the check — the earlier ``if resolved_active_homes and ...``
    # silently degraded to no-op when config was absent.  ``apply_report``
    # is now the single choke-point that refuses tree groups without
    # active_homes; this asserts the invariant locally so future callers
    # cannot bypass it.
    if not resolved_active_homes:
        raise ApplyError(
            "Refusing project-tree discard: no active_homes configured. "
            "Whole-directory discards require an active-home safety "
            "envelope.  Set active_homes in "
            "~/.config/duplicate_cleaner/config.toml before applying tree "
            "groups."
        )
    for m in group.members:
        if m.is_proposed_keeper or m.is_informational:
            continue
        path = Path(m.path)
        resolved = resolve_for_check(path)
        try:
            validate_not_excluded(resolved)
        except ValueError as e:
            raise ApplyError(
                f"Refusing to touch project-tree path from report: {e}"
            ) from e
        if not resolved.is_dir():
            raise ApplyError(
                f"Refusing project-tree discard: {resolved} is not a directory. "
                "Tree groups must reference the project root, not a file inside it."
            )
        if not any(is_within(resolved, r) for r in resolved_roots):
            raise ApplyError(
                "Refusing to touch project-tree path outside the recorded "
                f"scan roots: {resolved} "
                f"(roots: {[str(r) for r in resolved_roots]})"
            )
        if not any(is_within(resolved, h) for h in resolved_active_homes):
            raise ApplyError(
                "Refusing project-tree discard: "
                f"{resolved} is not inside any active home "
                f"({[str(h) for h in resolved_active_homes]}). "
                "Whole-directory discards require an active-home safety "
                "envelope."
            )
        if is_git_repo_dirty(resolved):
            raise ApplyError(
                "Refusing project-tree discard: "
                f"{resolved} contains a git repo with uncommitted changes. "
                "Commit or stash the working tree first, or drop this "
                "member from the report."
            )


def _validate_report_paths(
    report: Report,
    registry: AccountsRegistry | None = None,
    active_homes: list[Path] | None = None,
) -> list[Path]:
    """Reject any discard path that is excluded or outside the recorded roots.

    Returns the resolved list of scan roots for later re-use. Raises
    ``ApplyError`` on the first member that fails validation — zero side
    effects, so a bad report is caught before any move happens.

    v0.2 sub-phase 5b — Alt-C dispatch: each proposed-discard member is
    routed by ``source_id`` BEFORE any local exclusion rule runs.

    * ``source_id == "local"``: original v0.1.1 rails run unchanged
      (``resolve_for_check`` → ``validate_not_excluded`` → ``is_within``
      containment against the resolved scan roots).
    * ``source_id`` anything else: cloud rails run via
      :func:`paths.validate_cloud_entry` (source_id ∈ AccountsRegistry,
      shape-matching ``cloud_file_id``, non-empty ``etag``, ``is_shared``
      False).  The cloud ``Path`` is NEVER ``.resolve()``d — Alt-C keeps it
      as opaque display data only.

    G1: ``report.roots`` themselves are validated with the same rules as
    ``config.active_homes`` (``validate_scan_root_candidate``). A poisoned
    or hand-edited ``report.json`` that sets ``roots=["/"]`` or
    ``roots=["/Users"]`` is rejected here BEFORE any per-member containment
    check runs — otherwise every local discard path would pass ``is_within``
    against ``/``.  Cloud entries are unaffected: they do not consult roots.
    """
    if report.discover:
        raise ApplyError(
            "Refusing to apply: report is in --discover mode "
            "(no keepers proposed). Re-run `dc scan` without --discover to "
            "produce an actionable report."
        )
    if not report.roots:
        raise ApplyError(
            "Report has no scan roots recorded; refusing to apply. "
            "Re-run `dc scan` to produce a valid report."
        )
    for r in report.roots:
        try:
            validate_scan_root_candidate(Path(r))
        except ValueError as e:
            raise ApplyError(
                f"Refusing to apply: report.roots entry rejected — {e}"
            ) from e
    resolved_roots = [resolve_for_check(Path(r)) for r in report.roots]
    # A registry read is required for cloud dispatch.  Construction is cheap
    # (no disk I/O until .load()) so we build it here rather than making the
    # parameter mandatory — a local-only call site (every v0.1.1 test) does
    # not need to know about the registry.
    #
    # Audit pass 11: extract the authorised set ONCE at the top of the
    # validation loop so a big cloud-heavy report doesn't re-read
    # accounts.toml per member.
    reg: AccountsRegistry = registry if registry is not None else AccountsRegistry()
    authorized: set[str] = {"local"} | {e.id for e in reg.load()}
    resolved_active_homes: list[Path] | None = (
        [resolve_for_check(h) for h in active_homes] if active_homes else None
    )
    for g in _iter_all_groups(report):
        if g.kind == "tree":
            # v0.4: project-tree discards are directory paths.  Route them
            # through their dedicated validator BEFORE the per-file loop
            # inspects individual members — the file-level rails would
            # reject a directory as "not a virtual archive path" without
            # catching the important dirty-git / active-home rules.
            _validate_tree_group(g, resolved_roots, resolved_active_homes)
            continue
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            if m.source_id == "local":
                path_str = str(m.path)
                # v0.1.1: a virtual archive-member path (``outer.zip::inner``)
                # is NEVER a legal discard target. The only way to reclaim
                # bytes from a duplicated archive is to trash the whole outer
                # archive — which is a real on-disk path without ``::``.
                if is_virtual_archive_path(path_str):
                    raise ApplyError(
                        "Refusing to trash archive member: "
                        f"{path_str} contains the '{ARCHIVE_SEP}' virtual "
                        "path separator. Only whole archives may be discarded "
                        "— see the archive-whole groups."
                    )
                path = Path(m.path)
                # Cloud ``Path`` is never resolved.  Local dispatch runs the
                # v0.1.1 rails on real filesystem paths only — the dispatch
                # key above guarantees we never .resolve() a cloud path here.
                resolved = resolve_for_check(path)
                try:
                    validate_not_excluded(resolved)
                except ValueError as e:
                    raise ApplyError(
                        f"Refusing to touch path from report: {e}"
                    ) from e
                if not any(is_within(resolved, r) for r in resolved_roots):
                    raise ApplyError(
                        "Refusing to touch path outside the recorded scan "
                        f"roots: {resolved} "
                        f"(roots: {[str(r) for r in resolved_roots]})"
                    )
            else:
                # Cloud entry — Alt-C: never resolve the Path.  Enforce cloud
                # rails only.  The wire-up of Source.move_to_trash lands in
                # sub-phase 5c; until then _apply_report refuses the actual
                # move for any non-local source with a clear message.
                try:
                    validate_cloud_entry_with_authorized(m, authorized)
                except ValueError as e:
                    raise ApplyError(
                        f"Refusing cloud discard {m.path!r}: {e}"
                    ) from e
    return resolved_roots


def plan_moves(report: Report) -> list[tuple[Path, int, float, str]]:
    """Return [(path, size, mtime, hash), ...] for every LOCAL discard.

    v0.2 sub-phase 5b: cloud discards pass ``_validate_report_paths`` but
    are excluded from the local trash plan — the mover-to-source dispatch
    for cloud entries lands in sub-phase 5c.  Until then a cloud entry
    surfaces via ``_count_cloud_discards`` so the CLI can report "cloud
    discards deferred to sub-phase 5c".
    """
    moves: list[tuple[Path, int, float, str]] = []
    for g in _iter_all_groups(report):
        # v0.4: project-tree groups dispatch through ``plan_tree_moves``;
        # their members are directory paths, not files, and cannot flow
        # through the local file trash loop.
        if g.kind == "tree":
            continue
        # v0.7 / v0.8: image-near-dup, audio-near-dup, and video-near-dup
        # groups all flow through the same file-scoped ``send2trash``
        # rail as ``kind="exact"`` groups.  The dispatch is uniform per
        # non-tree group — see the class docstring and
        # ``schema.py::GroupKind`` for the documented contract.
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            if m.source_id != "local":
                continue
            moves.append((Path(m.path), m.size, m.mtime, m.hash))
    return moves


def plan_tree_moves(report: Report) -> list[tuple[Path, int, int, ReportMember]]:
    """Return [(project_root, total_bytes, identical_file_count, member), ...] for tree discards.

    v0.4: one entry per non-keeper member of every ``kind="tree"`` group.
    ``total_bytes`` is the aggregate size of the project directory (stamped
    on the ReportMember at scan time so we don't re-walk the tree during
    apply).  ``identical_file_count`` mirrors the group-level count for
    the manifest.
    """
    out: list[tuple[Path, int, int, ReportMember]] = []
    for g in report.groups:
        if g.kind != "tree":
            continue
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            out.append((Path(m.path), m.size, g.identical_file_count or 0, m))
    return out


def _count_cloud_discards(report: Report) -> int:
    """Count discards whose ``source_id`` is not ``"local"``.

    v0.2 sub-phase 5b: cloud discards flow through ``validate_cloud_entry``
    but not through ``Source.move_to_trash`` yet.  Kept for the existing
    5b test / result-dict shape.  Sub-phase 5c actually wires the dispatch
    so ``cloud_deferred`` is 0 whenever a ``sources`` map is provided.
    """
    n = 0
    for g in _iter_all_groups(report):
        if g.kind == "tree":
            continue
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            if m.source_id != "local":
                n += 1
    return n


def plan_cloud_moves(report: Report) -> list[ReportMember]:
    """Return every proposed-discard ``ReportMember`` with ``source_id != "local"``.

    v0.2 sub-phase 5c: symmetric to :func:`plan_moves`, but for the cloud
    dispatch loop.  Cloud paths are never resolved — the ``ReportMember`` is
    passed through so the mover can hand ``cloud_file_id`` / ``etag`` to
    :meth:`Source.check_drift` and :meth:`Source.move_to_trash` without
    round-tripping through ``Path`` semantics.
    """
    out: list[ReportMember] = []
    for g in _iter_all_groups(report):
        if g.kind == "tree":
            continue
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            if m.source_id == "local":
                continue
            out.append(m)
    return out


def _member_to_cloud_record(m: ReportMember) -> FileRecord:
    """Build a minimal ``FileRecord`` from a cloud ``ReportMember`` for source dispatch.

    Only the fields consumed by :meth:`Source.check_drift` / :meth:`Source.move_to_trash`
    are populated — ``inode`` / ``dev`` / ``nlink`` stay at 0 (unused for
    cloud dispatch).  The ``Path`` is passed through UNCHANGED — it is
    opaque display text on cloud records and MUST NOT be ``.resolve()``d.
    """
    return FileRecord(
        path=Path(m.path),
        size=m.size,
        mtime=m.mtime,
        inode=0,
        dev=0,
        nlink=1,
        source_id=m.source_id,
        etag=m.etag,
        cloud_file_id=m.cloud_file_id,
        owner=m.owner,
        is_shared=m.is_shared,
    )


def _verify_unchanged(path: Path, size: int, mtime: float) -> None:
    if not path.exists():
        raise PathChangedError(f"missing on disk: {path}")
    st = path.stat()
    if st.st_size != size:
        raise PathChangedError(
            f"size changed: {path} (was {size}, now {st.st_size})"
        )
    if abs(st.st_mtime - mtime) > 1e-3:
        raise PathChangedError(
            f"mtime changed: {path} (was {mtime}, now {st.st_mtime})"
        )


# The default trash callable — shared verbatim with LocalFileSystemSource
# via ``apply.trash.default_trash_fn`` so the send2trash + basename-diff
# implementation lives in exactly one place.  Kept as a module-level alias
# so ``apply_report(trash_fn=None)`` still resolves to a concrete callable.
_default_trash_fn = default_trash_fn


def _write_manifest(manifest_path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` to ``manifest_path`` with fsync durability.

    Writes to ``manifest_path.tmp``, fsyncs the file, renames over the
    target, then fsyncs the parent directory so the rename itself is
    durable across power loss.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, manifest_path)
    # Fsync the parent so the rename survives a crash before the OS flushes.
    try:
        dirfd = os.open(str(manifest_path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        # Not all filesystems support dir-fsync (e.g. some FUSE targets).
        pass
    finally:
        os.close(dirfd)


def apply_report(
    json_path: Path,
    *,
    commit: bool = False,
    runs_dir: Path = RUNS_DIR,
    trash_fn: TrashFn | None = None,
    registry: AccountsRegistry | None = None,
    sources: dict[str, Source] | None = None,
    active_homes: list[Path] | None = None,
) -> dict[str, Any]:
    """Verify then move discarded files to Trash. Dry-run unless commit=True.

    v0.2 sub-phase 5c: cloud discards are now dispatched to
    :meth:`Source.move_to_trash` via ``sources``.  ``sources`` maps
    ``source_id`` (e.g. ``"gdrive:personal"``, ``"onedrive:main"``) to a
    concrete :class:`Source` whose ``is_read_only_scan`` MUST be ``False``.
    ``None`` means local-only mode — a report carrying any cloud entry is
    refused with a clear ``ApplyError``.

    Semantics on cloud failures (per design doc §5.4):

    * :class:`SourceDriftError` (etag drift) — abort mid-apply. Manifest
      flushed so entries 1..(n-1) that already trashed are recoverable via
      ``dc undo``.  Same semantics as local size+mtime drift.
    * :class:`SourceAuthError` / :class:`SourceRateLimitError` — abort
      mid-apply.  Continuing under a bad token silently produces a manifest
      with many missed entries; better to fail loud with one action item.
    * Other :class:`SourceError` (not-found, permission) — logged per-entry,
      apply continues.  Same as ``OSError`` on local ``send2trash``.
    """
    report = load_report(json_path)
    # K1 (audit pass 14 ship-blocker): tree groups require active_homes.
    # Refuse up-front — before any per-member rail runs — so the caller
    # gets one clean actionable error instead of a per-member replay.
    # ``_validate_tree_group`` re-asserts the invariant locally as
    # defense in depth.
    has_tree_group = any(g.kind == "tree" for g in report.groups)
    if has_tree_group and not active_homes:
        raise ApplyError(
            "Refusing to apply: report contains project-tree discards but "
            "no active_homes are configured. Set active_homes in "
            "~/.config/duplicate_cleaner/config.toml before applying tree "
            "groups."
        )
    # Enforce exclusions and root containment against paths from the report
    # BEFORE anything else — a poisoned report must not cause side effects.
    # v0.2 sub-phase 5b: cloud entries flow through the cloud rails (Alt-C
    # dispatch on ``source_id``); local entries flow through the v0.1.1 rails.
    _validate_report_paths(report, registry=registry, active_homes=active_homes)

    moves = plan_moves(report)
    cloud_moves = plan_cloud_moves(report)
    tree_moves = plan_tree_moves(report)

    # v0.2 sub-phase 5c: local-only mode is signalled by ``sources=None``.
    # A cloud entry in the report is a hard refusal — the mover cannot
    # dispatch without an authorized sources map.
    if cloud_moves and sources is None:
        raise ApplyError(
            f"Refusing to apply: report contains {len(cloud_moves)} cloud "
            "discard(s) but no sources map was provided. Construct the "
            "sources dict from AccountsRegistry + TokenStore (see "
            "cli.py::apply) and pass it via sources=... — local-only mode "
            "cannot dispatch cloud discards."
        )

    # Pre-flight cloud checks — fail fast BEFORE any I/O so a mis-constructed
    # source (missing entry, is_read_only_scan=True) trips the tripwire
    # BEFORE any HTTP call.  Re-checked inside the move loop as defense in
    # depth in case a caller mutated the map mid-run.
    if cloud_moves:
        assert sources is not None  # narrowed above
        for m in cloud_moves:
            src = sources.get(m.source_id)
            if src is None:
                raise ApplyError(
                    f"Refusing to apply: cloud entry {m.path} has source_id "
                    f"{m.source_id!r} which is not in the sources map "
                    f"({sorted(sources.keys())}). Register the account "
                    "with `dc auth add` and rescan, or supply the source at "
                    "apply time."
                )
            if getattr(src, "is_read_only_scan", False):
                raise ApplyError(
                    f"Refusing to apply: source {m.source_id!r} was "
                    "constructed with is_read_only_scan=True.  This "
                    "tripwire fires BEFORE any HTTP call — the source "
                    "must be built with is_read_only_scan=False for the "
                    "trash dispatch."
                )

    verified: list[tuple[Path, int, float, str]] = []
    errors: list[str] = []
    for path, size, mtime, h in moves:
        try:
            _verify_unchanged(path, size, mtime)
        except PathChangedError as e:
            errors.append(str(e))
        else:
            verified.append((path, size, mtime, h))

    result: dict[str, Any] = {
        "planned": len(moves) + len(cloud_moves) + len(tree_moves),
        "planned_local": len(moves),
        "planned_cloud": len(cloud_moves),
        "planned_tree": len(tree_moves),
        "verified": len(verified) + len(cloud_moves) + len(tree_moves),
        "verified_local": len(verified),
        "verified_cloud": len(cloud_moves),
        "verified_tree": len(tree_moves),
        "changed_or_missing": errors,
        "committed": False,
        "manifest_path": None,
        "moved": 0,
        "moved_local": 0,
        "moved_cloud": 0,
        "moved_tree": 0,
        # v0.2 sub-phase 5c: cloud dispatch is wired.  When ``sources`` is
        # provided every cloud entry is dispatched (or logged as an error);
        # ``cloud_deferred`` stays for backcompat with the 5b result-dict
        # shape but is always 0 in 5c.
        "cloud_deferred": 0,
        # v0.2 sub-phase 5d: cloud entries skipped per-entry (SourceNotFound
        # / SourcePermission during check_drift or move_to_trash) are counted
        # here so the CLI can surface a "Skipped M cloud file(s)" line.
        # ``moved_cloud`` counts successful dispatches; ``skipped_cloud`` is
        # every log-and-continue path in the cloud dispatch loop.
        "skipped_cloud": 0,
        # K6 (audit pass 14): project-tree discards whose send2trash raises
        # a non-fatal ``OSError`` (log-and-continue) are counted here so
        # the CLI can surface a "Skipped N project tree(s)" line — mirrors
        # the ``skipped_cloud`` shape.  Zero on dry-run.
        "skipped_tree": 0,
    }

    if not commit:
        return result

    if errors:
        raise ApplyError(
            f"Refusing to commit: {len(errors)} path(s) changed since scan. "
            "Rescan and try again."
        )

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    # v0.2 sub-phase 5a: every entry now carries the trailing cloud fields
    # with their v0.1.1-equivalent defaults (``source_id="local"``, cloud
    # ids / etag null).  ``ManifestEntry.model_dump()`` guarantees the JSON
    # shape stays in sync with the pydantic model used by future sub-phases.
    # ``Manifest`` also stamps ``manifest_version="0.2.0"`` at the top level.
    entries: list[dict[str, Any]] = [
        ManifestEntry(
            original_path=str(p),
            size=s,
            mtime=mt,
            hash=h,
            trashed_at_path=None,
        ).model_dump()
        for p, s, mt, h in verified
    ]
    # v0.2 sub-phase 5c: cloud entries land in the same manifest, appended
    # after every local row so the ``entries`` list order matches the
    # dispatch order below.  ``trashed_at_path`` stays None (cloud discards
    # have no local Trash placement); ``cloud_trash_id`` gets stamped after
    # a successful move_to_trash returns its ``TrashedLocation``.
    cloud_entries_start = len(entries)
    for m in cloud_moves:
        entries.append(
            ManifestEntry(
                original_path=str(m.path),
                size=m.size,
                mtime=m.mtime,
                hash=m.hash,
                trashed_at_path=None,
                source_id=m.source_id,
                cloud_file_id=m.cloud_file_id,
                cloud_trash_id=None,
                etag=m.etag,
            ).model_dump()
        )
    # v0.4 project-tree entries.  Directory paths, not files; the mover
    # sends the whole directory to Trash and undo restores it wholesale.
    tree_entries_start = len(entries)
    for path, total_bytes, ident_count, member in tree_moves:
        entries.append(
            ManifestEntry(
                original_path=str(path),
                size=int(total_bytes),
                mtime=member.mtime,
                hash=member.hash,
                trashed_at_path=None,
                is_project_tree=True,
                identical_file_count=int(ident_count),
                project_tree_bytes=int(total_bytes),
            ).model_dump()
        )

    def _flush_manifest() -> None:
        manifest = Manifest(
            created_at=ts,
            roots=[str(r) for r in report.roots],
            entries=[ManifestEntry.model_validate(e) for e in entries],
        )
        _write_manifest(manifest_path, manifest.model_dump())

    # Write BEFORE moving anything, so recovery is always possible.
    _flush_manifest()

    tf = trash_fn or _default_trash_fn
    moved_local = 0
    moved_cloud = 0
    skipped_cloud = 0
    for i, (p, s, mt, _) in enumerate(verified):
        # Re-verify immediately before the mutation — the disk may have
        # changed between the batch verify and now.
        try:
            _verify_unchanged(p, s, mt)
        except PathChangedError as e:
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: {p} changed between verify and move ({e}). "
                f"Manifest at {manifest_path} reflects reality "
                f"({moved_local + moved_cloud} file(s) moved so far)."
            ) from e
        try:
            dest = tf(p)
        except OSError as e:
            log.error("Trash move failed for %s: %s", p, e)
            continue
        # G5: on ambiguity ``_default_trash_fn`` returns None; do NOT stamp
        # a guessed path into the manifest — leave it None so undo uses the
        # basename+hash fallback rather than trusting a wrong path.
        if dest is not None:
            entries[i]["trashed_at_path"] = str(dest)
        moved_local += 1
        # G6: flush after every successful move. Without this, a kernel
        # panic mid-loop leaves every entry with trashed_at_path=None on
        # disk and undo must rely entirely on the basename+hash fallback.
        _flush_manifest()

    # v0.2 sub-phase 5c: cloud dispatch loop.  Each iteration re-verifies
    # the source tripwire (defense in depth), calls ``check_drift`` (raises
    # SourceDriftError on etag mismatch → abort), then ``move_to_trash``.
    # ``SourceRateLimitError`` / ``SourceAuthError`` abort the run; other
    # SourceErrors (not-found, permission) are logged and skipped so a
    # single dead file doesn't torpedo the whole batch.
    for j, m in enumerate(cloud_moves):
        i = cloud_entries_start + j
        # sources is guaranteed non-None here (checked before commit block).
        assert sources is not None
        src = sources.get(m.source_id)
        if src is None:  # pragma: no cover - checked above too
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: source {m.source_id!r} disappeared "
                "from the sources map between pre-flight and dispatch."
            )
        if getattr(src, "is_read_only_scan", False):
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: source {m.source_id!r} became "
                "read-only.  Manifest at "
                f"{manifest_path} reflects reality "
                f"({moved_local + moved_cloud} file(s) moved so far)."
            )
        record = _member_to_cloud_record(m)
        # Drift check — raises SourceDriftError on etag mismatch, which we
        # promote to an ApplyError abort so semantics match local drift.
        try:
            src.check_drift(record)
        except SourceDriftError as e:
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: cloud etag drift for {m.path} ({e}). "
                f"Manifest at {manifest_path} reflects reality "
                f"({moved_local + moved_cloud} file(s) moved so far)."
            ) from e
        except SourceError as e:
            # A non-drift error surfacing from check_drift itself is a
            # provider-side problem — auth / rate / not-found.  Treat auth /
            # rate as abort, everything else as per-entry skip.
            if isinstance(e, SourceAuthError | SourceRateLimitError):
                _flush_manifest()
                raise ApplyError(
                    f"Aborted mid-apply: {type(e).__name__} during drift "
                    f"check for {m.path}: {e}. "
                    f"Manifest at {manifest_path} reflects reality "
                    f"({moved_local + moved_cloud} file(s) moved so far)."
                ) from e
            log.error("Drift-check failed for %s: %s", m.path, e)
            skipped_cloud += 1
            continue
        # Move to cloud trash.  tenacity retry is already inside the source
        # implementation; a SourceRateLimitError here is post-retry.
        try:
            loc = src.move_to_trash(record)
        except SourceDriftError as e:
            # Extremely unlikely but not impossible if the source implements
            # its own drift check inside move_to_trash.  Treat as abort.
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: cloud etag drift for {m.path} "
                f"during move_to_trash ({e}). "
                f"Manifest at {manifest_path} reflects reality "
                f"({moved_local + moved_cloud} file(s) moved so far)."
            ) from e
        except (SourceAuthError, SourceRateLimitError) as e:
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: {type(e).__name__} for {m.path}: {e}. "
                f"Manifest at {manifest_path} reflects reality "
                f"({moved_local + moved_cloud} file(s) moved so far)."
            ) from e
        except SourceError as e:
            log.error("Cloud trash failed for %s: %s", m.path, e)
            skipped_cloud += 1
            continue
        # TrashedLocation might carry an updated cloud_trash_id (Drive keeps
        # the same id; OneDrive Personal too).  Record both.
        entries[i]["cloud_trash_id"] = loc.cloud_trash_id
        if loc.cloud_file_id:
            entries[i]["cloud_file_id"] = loc.cloud_file_id
        moved_cloud += 1
        # G6 (cloud mirror): flush after every successful move.
        _flush_manifest()

    # v0.4 project-tree dispatch loop.  Each tree discard is a directory
    # path sent whole to Trash via the same trash_fn.  Order is AFTER
    # local and cloud files so a directory-level failure cannot orphan
    # cheaper reversible moves that already succeeded.
    moved_tree = 0
    skipped_tree = 0
    for k, (dir_path, _bytes, _ident, _member) in enumerate(tree_moves):
        i = tree_entries_start + k
        if not dir_path.is_dir():
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: project-tree {dir_path} disappeared "
                "between validate and move. "
                f"Manifest at {manifest_path} reflects reality "
                f"({moved_local + moved_cloud + moved_tree} file(s) moved so far)."
            )
        if is_git_repo_dirty(dir_path):
            _flush_manifest()
            raise ApplyError(
                f"Aborted mid-apply: project-tree {dir_path} became dirty "
                "between validate and move (uncommitted changes present). "
                f"Manifest at {manifest_path} reflects reality "
                f"({moved_local + moved_cloud + moved_tree} file(s) moved so far)."
            )
        try:
            dest = tf(dir_path)
        except OSError as e:
            # K6 (audit pass 14): count log-and-continue tree failures so
            # the CLI can surface them.  Without this counter the manifest
            # row for the failed tree keeps ``trashed_at_path=None`` (undo
            # already refuses to restore that shape) while ``moved_tree``
            # under-reports — silent failure.
            log.error("Trash move failed for project tree %s: %s", dir_path, e)
            skipped_tree += 1
            continue
        if dest is not None:
            entries[i]["trashed_at_path"] = str(dest)
        moved_tree += 1
        _flush_manifest()

    _flush_manifest()

    result["manifest_path"] = str(manifest_path)
    result["moved"] = moved_local + moved_cloud + moved_tree
    result["moved_local"] = moved_local
    result["moved_cloud"] = moved_cloud
    result["moved_tree"] = moved_tree
    result["skipped_cloud"] = skipped_cloud
    result["skipped_tree"] = skipped_tree
    result["committed"] = True
    return result
