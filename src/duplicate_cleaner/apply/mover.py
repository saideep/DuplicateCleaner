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
from duplicate_cleaner.paths import (
    is_within,
    resolve_for_check,
    validate_cloud_entry,
    validate_not_excluded,
    validate_scan_root_candidate,
)
from duplicate_cleaner.report.schema import Manifest, ManifestEntry, Report

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


def _validate_report_paths(
    report: Report,
    registry: AccountsRegistry | None = None,
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
    reg: AccountsRegistry = registry if registry is not None else AccountsRegistry()
    for g in report.groups:
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
                    validate_cloud_entry(m, reg)
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
    for g in report.groups:
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            if m.source_id != "local":
                continue
            moves.append((Path(m.path), m.size, m.mtime, m.hash))
    return moves


def _count_cloud_discards(report: Report) -> int:
    """Count discards whose ``source_id`` is not ``"local"``.

    v0.2 sub-phase 5b: cloud discards flow through ``validate_cloud_entry``
    but not through ``Source.move_to_trash`` yet.  Used to surface a
    deferred-count in the apply result summary.
    """
    n = 0
    for g in report.groups:
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            if m.source_id != "local":
                n += 1
    return n


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
) -> dict[str, Any]:
    """Verify then move discarded files to Trash. Dry-run unless commit=True."""
    report = load_report(json_path)
    # Enforce exclusions and root containment against paths from the report
    # BEFORE anything else — a poisoned report must not cause side effects.
    # v0.2 sub-phase 5b: cloud entries flow through the cloud rails (Alt-C
    # dispatch on ``source_id``); local entries flow through the v0.1.1 rails.
    _validate_report_paths(report, registry=registry)

    # v0.2 sub-phase 5b: cloud discards are validated but NOT yet dispatched
    # to Source.move_to_trash — that lands in sub-phase 5c.  The count is
    # surfaced in the result dict so the CLI can print an accurate "deferred"
    # message rather than silently dropping them.
    cloud_deferred = _count_cloud_discards(report)
    # TODO(sub-phase 5c): wire Source.move_to_trash here.  Iterate cloud
    # discards, look up the ``Source`` for ``member.source_id`` in the
    # ``sources_by_id`` map, run ``_pre_trash_drift_check`` (etag re-fetch),
    # then call ``src.move_to_trash(record)`` and merge the returned
    # ``TrashedLocation`` into the manifest row.  See design doc §5.

    moves = plan_moves(report)

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
        "planned": len(moves),
        "verified": len(verified),
        "changed_or_missing": errors,
        "committed": False,
        "manifest_path": None,
        "moved": 0,
        # v0.2 sub-phase 5b: cloud discards were validated but not moved.
        "cloud_deferred": cloud_deferred,
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
    moved = 0
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
                f"({moved} file(s) moved so far)."
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
        moved += 1
        # G6: flush after every successful move. Without this, a kernel
        # panic mid-loop leaves every entry with trashed_at_path=None on
        # disk and undo must rely entirely on the basename+hash fallback.
        _flush_manifest()

    _flush_manifest()

    result["manifest_path"] = str(manifest_path)
    result["moved"] = moved
    result["committed"] = True
    return result
