"""Move discarded duplicates to Trash — dry-run by default, always writes an undo manifest first."""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import send2trash  # type: ignore[import-untyped]

from duplicate_cleaner.paths import (
    is_within,
    resolve_for_check,
    trash_dir_for,
    validate_not_excluded,
    validate_scan_root_candidate,
)
from duplicate_cleaner.report.schema import Report

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


def _validate_report_paths(report: Report) -> list[Path]:
    """Reject any discard path that is excluded or outside the recorded roots.

    Returns the resolved list of scan roots for later re-use. Raises
    ``ApplyError`` on the first path that fails validation — zero side
    effects, so a bad report is caught before any move happens.

    G1: ``report.roots`` themselves are validated with the same rules as
    ``config.active_homes`` (``validate_scan_root_candidate``). A poisoned
    or hand-edited ``report.json`` that sets ``roots=["/"]`` or
    ``roots=["/Users"]`` is rejected here BEFORE any per-member containment
    check runs — otherwise every discard path would pass ``is_within``
    against ``/``.
    """
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
    for g in report.groups:
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            path = Path(m.path)
            resolved = resolve_for_check(path)
            try:
                validate_not_excluded(resolved)
            except ValueError as e:
                raise ApplyError(
                    f"Refusing to touch path from report: {e}"
                ) from e
            if not any(is_within(resolved, r) for r in resolved_roots):
                raise ApplyError(
                    "Refusing to touch path outside the recorded scan roots: "
                    f"{resolved} (roots: {[str(r) for r in resolved_roots]})"
                )
    return resolved_roots


def plan_moves(report: Report) -> list[tuple[Path, int, float, str]]:
    """Return [(path, size, mtime, hash), ...] for every path proposed for discard."""
    moves: list[tuple[Path, int, float, str]] = []
    for g in report.groups:
        for m in g.members:
            if m.is_proposed_keeper or m.is_informational:
                continue
            moves.append((Path(m.path), m.size, m.mtime, m.hash))
    return moves


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


def _default_trash_fn(path: Path) -> Path | None:
    """Trash ``path`` and best-effort record where it landed.

    ``send2trash`` returns nothing, so we snapshot the volume-appropriate
    Trash directory before and after the call and diff the basename set to
    pick the destination. On ambiguity (0 or >1 new entries) we return
    ``None`` and the caller records ``trashed_at_path=None``; undo falls
    back to a name+hash+size scan of the Trash directory (see
    ``apply.undo._locate_by_basename_and_size``).
    """
    trash = trash_dir_for(path)
    before: set[str] = set()
    if trash.exists():
        before = {p.name for p in trash.iterdir()}
    send2trash.send2trash(str(path))
    after: set[str] = set()
    if trash.exists():
        after = {p.name for p in trash.iterdir()}
    new = after - before
    if len(new) == 1:
        return trash / next(iter(new))
    # Ambiguous — record None so callers do not stamp a specific-but-wrong
    # path into the manifest. Undo will locate the file by basename+hash.
    return None


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
) -> dict[str, Any]:
    """Verify then move discarded files to Trash. Dry-run unless commit=True."""
    report = load_report(json_path)
    # Enforce exclusions and root containment against paths from the report
    # BEFORE anything else — a poisoned report must not cause side effects.
    _validate_report_paths(report)

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
    entries: list[dict[str, Any]] = [
        {
            "original_path": str(p),
            "size": s,
            "mtime": mt,
            "hash": h,
            "trashed_at_path": None,
        }
        for p, s, mt, h in verified
    ]

    def _flush_manifest() -> None:
        _write_manifest(
            manifest_path, {"created_at": ts, "entries": entries}
        )

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
