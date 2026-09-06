"""Move files to their organize destinations — dry-run by default, undo-manifest-backed.

Mirrors the safety envelope of :mod:`duplicate_cleaner.apply.mover` for the
dedup mover: dry-run default, ``--commit`` gate, atomic manifest write BEFORE
any move, per-file re-verify against the plan's size+mtime, cohesive units
move atomically or refuse. ``shutil.move`` is deliberately NOT used here — it
stays confined to :mod:`duplicate_cleaner.apply.undo` and
:mod:`duplicate_cleaner.organize.undo`. Same-volume moves use ``os.rename``;
cross-volume moves use ``shutil.copy2`` + ``send2trash`` so the source stays
recoverable via Trash if anything goes wrong post-copy.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import blake3  # type: ignore[import-untyped]
import send2trash  # type: ignore[import-untyped]

from duplicate_cleaner.config import Config, load_config
from duplicate_cleaner.organize.plan import PlanEntry, PlanFile
from duplicate_cleaner.paths import (
    is_within,
    resolve_for_check,
    validate_not_excluded,
)

log = logging.getLogger(__name__)

# Organize runs live under a distinct subtree so a `dc undo` operator does
# not confuse a dedup manifest with an organize manifest.
RUNS_DIR = Path.home() / ".local" / "share" / "duplicate_cleaner" / "organize-runs"


class OrganizeApplyError(RuntimeError):
    """Raised when organize apply cannot safely proceed."""


class OrganizeDriftError(OrganizeApplyError):
    """Source file size or mtime changed between discover and apply."""


@dataclass
class ApplyPlanResult:
    """Return value from :func:`apply_plan` — mirrors the dedup mover shape."""

    committed: bool = False
    planned: int = 0
    verified: int = 0
    moved: int = 0
    manifest_path: Path | None = None
    dest_root: Path | None = None
    collisions: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_plan(plan_path: Path) -> PlanFile:
    """Deserialize a PlanFile from JSON on disk."""
    data = json.loads(plan_path.read_text())
    return PlanFile.model_validate(data)


def _path_hash8(path: Path) -> str:
    """First 8 hex chars of the BLAKE3 digest of the source path string."""
    h = blake3.blake3(str(path).encode("utf-8"))
    return str(h.hexdigest())[:8]


def _folder_of(dest_str: str) -> str:
    """Return the folder portion of a POSIX-style ``proposed_dest`` string."""
    return str(PurePosixPath(dest_str).parent)


def _validate_dest_root(dest_root: Path, config: Config) -> Path:
    """Reject a dest_root that is excluded or (when active_homes is set) outside them."""
    resolved = resolve_for_check(dest_root)
    try:
        validate_not_excluded(resolved)
    except ValueError as e:
        raise OrganizeApplyError(
            f"Refusing to apply: dest_root {dest_root} is inside an excluded "
            f"location: {e}"
        ) from e
    if config.active_homes:
        allowed = [resolve_for_check(h) for h in config.active_homes]
        if not any(is_within(resolved, h) for h in allowed):
            raise OrganizeApplyError(
                f"Refusing to apply: dest_root {dest_root} is not inside any "
                f"configured active_home ({[str(h) for h in allowed]})."
            )
    return resolved


def _validate_proposed_dest(entry: PlanEntry) -> PurePosixPath:
    """Reject absolute or ``..``-carrying proposed_dest strings."""
    dest = PurePosixPath(entry.proposed_dest)
    if dest.is_absolute():
        raise OrganizeApplyError(
            f"Refusing to apply: proposed_dest {entry.proposed_dest!r} is "
            "absolute; must be relative to dest_root."
        )
    if ".." in dest.parts:
        raise OrganizeApplyError(
            f"Refusing to apply: proposed_dest {entry.proposed_dest!r} "
            "contains a '..' traversal segment."
        )
    return dest


def _validate_source_path(
    entry: PlanEntry, resolved_roots: list[Path] | None = None
) -> Path:
    """Reject a plan entry whose source_path is inside an excluded root.

    Audit pass 13 finding: also refuse a source_path outside ``plan.roots``.
    Symmetric with ``apply/mover.py::_validate_report_paths`` which enforces
    ``is_within(root)`` for every discard. Without this a hand-edited plan
    could route arbitrary user files (~/.ssh/config, ~/Documents/tax.pdf)
    into the organize tree without ever appearing in the scan output.
    """
    if entry.source_id != "local":
        raise OrganizeApplyError(
            f"Refusing to apply: entry source_id {entry.source_id!r} is not "
            "'local'; cross-source organize is deferred to sub-milestone 5.3-g."
        )
    resolved = resolve_for_check(entry.source_path)
    try:
        validate_not_excluded(resolved)
    except ValueError as e:
        raise OrganizeApplyError(
            f"Refusing to apply: source_path {entry.source_path} rejected: {e}"
        ) from e
    if resolved_roots:
        if not any(_is_within(resolved, r) for r in resolved_roots):
            raise OrganizeApplyError(
                f"Refusing to apply: source_path {entry.source_path} is not "
                f"inside any plan.roots entry "
                f"({[str(r) for r in resolved_roots]}). A plan cannot move "
                "files that were not part of its scan."
            )
    return resolved


def _is_within(child: Path, root: Path) -> bool:
    """True when ``child`` is at or below ``root`` (already-resolved paths)."""
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def _apply_rename_policy(entry: PlanEntry, config: Config) -> str:
    """Return the destination filename per ``config.rename_policy``.

    Default is ``preserve`` — filename bytes are never mutated.  ``date_prefix``
    prepends ``YYYY-MM-DD_`` derived from the entry's mtime; ``date_event_prefix``
    is treated the same for now (event-date lookup would require the cohesion
    group's date span, which is not yet on the plan entry — resolves cleanly to
    ``date_prefix`` for isolated files).
    """
    if config.rename_policy == "preserve":
        return entry.filename
    dt = datetime.fromtimestamp(entry.mtime, tz=UTC)
    prefix = dt.date().isoformat()
    return f"{prefix}_{entry.filename}"


def _verify_source_unchanged(entry: PlanEntry, path: Path) -> None:
    """Raise OrganizeDriftError if the source's size/mtime drifted from the plan."""
    if not path.exists():
        raise OrganizeDriftError(f"missing on disk: {path}")
    st = path.stat()
    if st.st_size != entry.size:
        raise OrganizeDriftError(
            f"size changed: {path} (plan {entry.size}, now {st.st_size})"
        )
    if abs(st.st_mtime - entry.mtime) > 1e-3:
        raise OrganizeDriftError(
            f"mtime changed: {path} (plan {entry.mtime}, now {st.st_mtime})"
        )


def _same_volume(a: Path, b: Path) -> bool:
    """True when both paths live on the same st_dev (rename is atomic)."""
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def _resolve_collision(
    dest: Path,
    entry: PlanEntry,
    collisions: list[dict[str, str]],
) -> Path:
    """Rename a colliding dest by appending ``_<hash8>`` to the stem."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    hash8 = _path_hash8(entry.source_path)
    renamed = dest.parent / f"{stem}_{hash8}{suffix}"
    collisions.append(
        {
            "source_path": str(entry.source_path),
            "original_dest": str(dest),
            "renamed_to": str(renamed),
        }
    )
    return renamed


def _check_cohesion(
    plan: PlanFile,
    *,
    split_cohesive_units: bool,
    warnings: list[str],
) -> None:
    """Refuse the run if any cohesion group's members target different folders.

    ``split_cohesive_units=True`` downgrades the refusal to a warning per the
    v0.3 invariant: cohesive units move atomically OR the operator explicitly
    signalled they want a split.
    """
    entries_by_group: dict[str, list[PlanEntry]] = {}
    for entry in plan.entries:
        gid = entry.cohesion_group_id
        if gid is None:
            continue
        entries_by_group.setdefault(gid, []).append(entry)

    violations: list[str] = []
    for gid, members in entries_by_group.items():
        if len(members) < 2:
            continue
        folders = {_folder_of(m.proposed_dest) for m in members}
        if len(folders) == 1:
            continue
        split_members = [
            f"{m.source_path} → {m.proposed_dest}" for m in members
        ]
        violations.append(
            f"cohesion group {gid!r} split across {sorted(folders)}: "
            + ", ".join(split_members)
        )

    if not violations:
        return
    if split_cohesive_units:
        for v in violations:
            warnings.append(f"cohesion split accepted (--split-cohesive-units): {v}")
            log.warning("Cohesion split accepted: %s", v)
        return
    raise OrganizeApplyError(
        "Refusing to apply: cohesive unit(s) would be split. Pass "
        "--split-cohesive-units to override. Violations:\n  - "
        + "\n  - ".join(violations)
    )


def _write_manifest(manifest_path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` — tempfile + fsync + os.replace + dir fsync.

    Mirrors :func:`duplicate_cleaner.apply.mover._write_manifest`.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, manifest_path)
    try:
        dirfd = os.open(str(manifest_path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)


def _do_move(
    source: Path,
    dest: Path,
) -> bool:
    """Move source→dest safely.  Returns True if cross-volume (source now in Trash).

    Same-volume: ``os.rename`` — atomic on POSIX.
    Cross-volume: ``shutil.copy2`` + ``send2trash(source)`` so the source
    remains recoverable if the operator changes their mind post-copy.  We
    deliberately do NOT use ``shutil.move`` here — that primitive stays
    confined to :mod:`organize.undo` / :mod:`apply.undo`.
    """
    # Parent directory guaranteed to exist by caller (created earlier via
    # ``os.makedirs`` using ``organize_dir_mode``).
    if _same_volume(source, dest.parent):
        os.rename(source, dest)
        return False
    # Cross-volume: import shutil locally to keep the module-level surface
    # narrow — a top-level ``import shutil`` would tempt future edits to
    # reach for ``shutil.move``.
    import shutil

    shutil.copy2(str(source), str(dest))
    # Post-copy sanity check against the copied file's size.
    src_size = source.stat().st_size
    dst_size = dest.stat().st_size
    if src_size != dst_size:
        # Best-effort cleanup: send the botched copy to Trash so the
        # operator can inspect it, then abort.  ``send2trash`` on a
        # freshly-created copy is safe — the source is still on disk.
        send2trash.send2trash(str(dest))
        raise OrganizeApplyError(
            f"Refusing to trust cross-volume copy: {source} ({src_size} B) "
            f"copied to {dest} came out at {dst_size} B."
        )
    # Audit pass 13 finding #1: content verify. Size-only means a silent
    # bit flip during copy2 (bad USB cable / RAM ECC event / driver bug)
    # leaves a corrupted copy at dest while we trash the good source.
    # BLAKE3 both files chunk-by-chunk before trashing the source — same
    # streaming pattern the reconcile pipeline uses.
    import blake3  # type: ignore[import-untyped]

    _CHUNK = 1 << 20
    src_hash = blake3.blake3()
    with source.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            src_hash.update(chunk)
    dst_hash = blake3.blake3()
    with dest.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            dst_hash.update(chunk)
    if src_hash.hexdigest() != dst_hash.hexdigest():
        send2trash.send2trash(str(dest))
        raise OrganizeApplyError(
            f"Refusing to trust cross-volume copy: {source} content hash "
            f"differs from {dest} after copy — botched copy trashed, "
            f"source is untouched."
        )
    send2trash.send2trash(str(source))
    return True


def apply_plan(
    plan_path: Path,
    *,
    commit: bool = False,
    split_cohesive_units: bool = False,
    config: Config | None = None,
    runs_dir: Path | None = None,
) -> ApplyPlanResult:
    """Apply an organize plan — dry-run by default, ``commit=True`` moves files.

    Safety rails (each one is a load-bearing invariant per ``docs/AUDIT_LOG.md``):

    * dry-run default — ``commit=False`` returns a plan summary and touches
      nothing on disk;
    * cohesive units move atomically or the run is refused unless
      ``split_cohesive_units=True``;
    * every ``source_path`` and effective ``dest`` is re-validated against
      ``EXCLUDED_ROOTS`` and the ``../`` traversal check;
    * manifest is written BEFORE the first move (tempfile + fsync + os.replace
      + parent-dir fsync);
    * per-file re-verify (size + mtime) fires immediately before the move —
      drift aborts the whole run;
    * collisions produce a ``_<hash8>`` suffix, never an overwrite.
    """
    cfg = config if config is not None else load_config()
    plan = load_plan(plan_path)

    if plan.dest_root is None:
        raise OrganizeApplyError(
            "Refusing to apply: plan has no dest_root recorded. "
            "Re-run `dc organize discover --dest <dir>` to produce a valid plan."
        )
    dest_root_resolved = _validate_dest_root(plan.dest_root, cfg)

    warnings: list[str] = []
    _check_cohesion(
        plan,
        split_cohesive_units=split_cohesive_units,
        warnings=warnings,
    )

    # Audit pass 13 finding #2/#9: resolve and validate every plan.root
    # BEFORE the entry loop so source_path can be constrained to a real
    # scan root. Same validator active_homes + report.roots use — refuses
    # `/`, ≤2-segment paths, non-existent, and inside EXCLUDED_ROOTS.
    from duplicate_cleaner.paths import validate_scan_root_candidate

    resolved_plan_roots: list[Path] = []
    for r in plan.roots:
        r_path = Path(r) if isinstance(r, str) else r
        try:
            validate_scan_root_candidate(r_path)
        except ValueError as e:
            raise OrganizeApplyError(
                f"Refusing to apply: plan.roots entry {r!r} rejected — {e}"
            ) from e
        resolved_plan_roots.append(resolve_for_check(r_path))

    # Pre-flight — validate every entry BEFORE any move.
    prepared: list[tuple[PlanEntry, Path, Path]] = []
    for entry in plan.entries:
        src_resolved = _validate_source_path(entry, resolved_plan_roots)
        rel = _validate_proposed_dest(entry)
        # Apply rename policy to the *filename* segment only — the domain +
        # subfolder are user-territory and left alone.
        new_name = _apply_rename_policy(entry, cfg)
        # Rebuild the relative destination with any renamed leaf.
        rel_parts = [*rel.parts[:-1], new_name]
        rel_final = PurePosixPath(*rel_parts)
        # ``rel_final`` is guaranteed relative (rel.is_absolute() checked
        # above), so joining under dest_root_resolved is safe.  Resolve to
        # normalize any symlinked parent directory so the containment check
        # sees the true on-disk location.
        dest_abs = resolve_for_check(dest_root_resolved / rel_final)
        # Belt-and-braces: verify the joined path still sits inside dest_root
        # even if a Unicode-normalisation prank slipped a ``..``-alike through.
        if not is_within(dest_abs, dest_root_resolved):
            raise OrganizeApplyError(
                f"Refusing to apply: computed dest {dest_abs} escapes "
                f"dest_root {dest_root_resolved}."
            )
        try:
            validate_not_excluded(dest_abs)
        except ValueError as e:
            raise OrganizeApplyError(
                f"Refusing to apply: dest {dest_abs} rejected: {e}"
            ) from e
        prepared.append((entry, src_resolved, dest_abs))

    result = ApplyPlanResult(
        planned=len(prepared),
        dest_root=dest_root_resolved,
        warnings=warnings,
    )

    if not commit:
        # Dry-run: verify the plan against current disk state without writing.
        verified = 0
        for entry, src_resolved, _dest in prepared:
            try:
                _verify_source_unchanged(entry, src_resolved)
            except OrganizeDriftError as e:
                result.errors.append(str(e))
            else:
                verified += 1
        result.verified = verified
        return result

    # ---- commit path ------------------------------------------------------
    runs_root = runs_dir if runs_dir is not None else RUNS_DIR
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_root / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"

    # Pre-verify every source in one pass so a stale plan aborts BEFORE any
    # side effect.  Mirrors ``apply_report``'s early size+mtime pass.
    for entry, src_resolved, _dest in prepared:
        _verify_source_unchanged(entry, src_resolved)
    result.verified = len(prepared)

    entries_out: list[dict[str, Any]] = []
    collisions: list[dict[str, str]] = []

    def _flush_manifest() -> None:
        _write_manifest(
            manifest_path,
            {
                "version": "0.3.0",
                "kind": "organize",
                "created_at": ts,
                "dest_root": str(dest_root_resolved),
                "roots": [str(r) for r in plan.roots],
                "entries": list(entries_out),
                "collisions": list(collisions),
            },
        )

    # Manifest is written BEFORE the first move so a crash mid-loop is still
    # recoverable via ``dc organize undo``.
    _flush_manifest()

    for entry, src_resolved, dest_abs in prepared:
        # Immediately re-verify — drift between the batch pre-verify above
        # and now aborts the whole run.  Semantics match the dedup mover.
        _verify_source_unchanged(entry, src_resolved)

        dest_abs.parent.mkdir(mode=cfg.organize_dir_mode, parents=True, exist_ok=True)
        final_dest = _resolve_collision(dest_abs, entry, collisions)

        try:
            cross_volume = _do_move(src_resolved, final_dest)
        except OSError as e:
            _flush_manifest()
            raise OrganizeApplyError(
                f"Move failed for {src_resolved} → {final_dest}: {e}. "
                f"Manifest at {manifest_path} reflects reality "
                f"({result.moved} file(s) moved so far)."
            ) from e

        entries_out.append(
            {
                "source_path": str(entry.source_path),
                "source_resolved": str(src_resolved),
                "dest_path": str(final_dest),
                "size": entry.size,
                "mtime": entry.mtime,
                "cohesion_group_id": entry.cohesion_group_id,
                "cross_volume": cross_volume,
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        result.moved += 1
        _flush_manifest()

    result.collisions = collisions
    result.manifest_path = manifest_path
    result.committed = True
    return result
