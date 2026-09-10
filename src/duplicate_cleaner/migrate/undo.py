"""Reverse a migration run — restore source originals, trash destination copies.

v0.5-b: reads a manifest emitted by :func:`execute_migration` (and possibly
extended by :func:`cleanup_source_after_migration`) and, per entry:

- ``cleanup_done=True``: restore the source original via
  :meth:`Source.restore_from_trash` (source cloud trash → source live tree).
- ``state="done"`` (whether or not cleanup ran): trash the destination copy
  via :meth:`Source.move_to_trash` so a re-planned migration does not
  double-copy.
- Any other state (``pending`` / ``skipped`` / ``error``): nothing to undo.

Per-entry error tolerant.  :class:`SourceAuthError` /
:class:`SourceRateLimitError` abort the run so a bad token cannot silently
miss most of the batch.  Other :class:`SourceError` (not-found, permission)
are logged per-entry and the run continues.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from duplicate_cleaner.migrate.mover import _flush_manifest, _load_manifest
from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    Source,
    SourceAuthError,
    SourceError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
    TrashedLocation,
)

log = logging.getLogger(__name__)


class UndoMigrationError(RuntimeError):
    """Raised when the undo cannot safely proceed (auth/rate abort)."""


@dataclass
class UndoResult:
    """Return shape for :func:`undo_migration`."""

    total: int = 0
    restored_source: int = 0
    trashed_dest: int = 0
    errors: list[str] = field(default_factory=list)
    manifest_path: Path | None = None


def undo_migration(
    manifest_path: Path,
    *,
    sources_by_id: dict[str, Source],
) -> UndoResult:
    """Restore source originals and trash destination copies.

    ``sources_by_id`` MUST contain both the source and destination Sources
    with ``is_read_only_scan=False`` — the same tripwire that guards
    ``dc migrate copy`` fires here.
    """
    manifest = _load_manifest(manifest_path)
    result = UndoResult(total=len(manifest.entries), manifest_path=manifest_path)

    src_source = sources_by_id.get(manifest.plan_source_id)
    dst_source = sources_by_id.get(manifest.plan_dest_id)
    if src_source is None or dst_source is None:
        missing = [
            sid
            for sid, present in (
                (manifest.plan_source_id, src_source),
                (manifest.plan_dest_id, dst_source),
            )
            if present is None
        ]
        raise UndoMigrationError(
            f"Refusing undo: source(s) {missing!r} not in sources_by_id "
            f"({sorted(sources_by_id.keys())})."
        )
    if getattr(src_source, "is_read_only_scan", False) or getattr(
        dst_source, "is_read_only_scan", False
    ):
        raise UndoMigrationError(
            "Refusing undo: at least one source is read-only. Construct with "
            "is_read_only_scan=False for `dc migrate undo`."
        )

    for i, entry in enumerate(manifest.entries):
        # 1. If cleanup ran, restore the source original first — the
        # user-visible "undo" reverses cleanup + copy in the reverse order
        # the copy loop wrote them.
        if entry.cleanup_done:
            # Audit pass 15 finding #1 (BLOCK): reject cleanup_done rows
            # with a null/empty source_cloud_trash_id. Silently falling back
            # to source_file_id would let a poisoned manifest coerce
            # restore_from_trash into un-trashing a file another client
            # intentionally trashed. Mirrors apply/undo.py's null-trash-id
            # rejection (v0.2 sub-phase 5d invariant).
            if not entry.source_cloud_trash_id:
                result.errors.append(
                    f"Refuse to restore source for {entry.source_path}: "
                    f"cleanup_done=True but source_cloud_trash_id is null — "
                    "manifest is corrupt or hand-edited; nothing to un-trash."
                )
                _flush_manifest(manifest, manifest_path)
                continue
            loc = TrashedLocation(
                source_id=entry.source_id,
                original_path=entry.source_path,
                cloud_file_id=entry.source_file_id,
                cloud_trash_id=entry.source_cloud_trash_id,
            )
            try:
                src_source.restore_from_trash(loc)
            except (SourceAuthError, SourceRateLimitError) as exc:
                _flush_manifest(manifest, manifest_path)
                raise UndoMigrationError(
                    f"Aborted undo: {type(exc).__name__} restoring source "
                    f"{entry.source_path}: {exc}. "
                    f"Manifest at {manifest_path} reflects reality "
                    f"({result.restored_source} source original(s) restored "
                    f"and {result.trashed_dest} dest copy(ies) trashed so far)."
                ) from exc
            except (SourceNotFoundError, SourcePermissionError) as exc:
                log.warning(
                    "Undo: source restore skipped for %s: %s",
                    entry.source_path,
                    exc,
                )
                result.errors.append(f"{entry.source_path}: source restore: {exc}")
            except SourceError as exc:
                log.warning(
                    "Undo: source restore failed for %s: %s",
                    entry.source_path,
                    exc,
                )
                result.errors.append(f"{entry.source_path}: source restore: {exc}")
            else:
                manifest.entries[i] = manifest.entries[i].model_copy(
                    update={"cleanup_done": False, "source_cloud_trash_id": None}
                )
                result.restored_source += 1
                _flush_manifest(manifest, manifest_path)

        # 2. If a destination copy was made, trash it.  Uses the manifest's
        # stamped dest_cloud_file_id / dest_etag so we address the exact
        # object the copy loop wrote — not a fresh list_files sweep.
        entry_now = manifest.entries[i]
        if entry_now.state == "done" and entry_now.dest_cloud_file_id:
            record = FileRecord(
                path=Path(entry_now.dest_expected_path),
                size=int(entry_now.source_size),
                mtime=0.0,
                inode=0,
                dev=0,
                nlink=1,
                source_id=manifest.plan_dest_id,
                cloud_file_id=entry_now.dest_cloud_file_id,
                etag=entry_now.dest_etag,
            )
            try:
                dst_source.move_to_trash(record)
            except (SourceAuthError, SourceRateLimitError) as exc:
                _flush_manifest(manifest, manifest_path)
                raise UndoMigrationError(
                    f"Aborted undo: {type(exc).__name__} trashing dest for "
                    f"{entry_now.source_path}: {exc}. "
                    f"Manifest at {manifest_path} reflects reality "
                    f"({result.restored_source} source original(s) restored "
                    f"and {result.trashed_dest} dest copy(ies) trashed so far)."
                ) from exc
            except (SourceNotFoundError, SourcePermissionError) as exc:
                log.warning(
                    "Undo: dest trash skipped for %s: %s",
                    entry_now.source_path,
                    exc,
                )
                result.errors.append(
                    f"{entry_now.source_path}: dest trash: {exc}"
                )
            except SourceError as exc:
                log.warning(
                    "Undo: dest trash failed for %s: %s",
                    entry_now.source_path,
                    exc,
                )
                result.errors.append(
                    f"{entry_now.source_path}: dest trash: {exc}"
                )
            else:
                manifest.entries[i] = entry_now.model_copy(
                    update={
                        "state": "pending",
                        "dest_cloud_file_id": None,
                        "dest_etag": None,
                        "uploaded_hash": None,
                        "uploaded_hash_algo": None,
                        "verified": False,
                        "verified_ts": None,
                    }
                )
                result.trashed_dest += 1
                _flush_manifest(manifest, manifest_path)

    return result
