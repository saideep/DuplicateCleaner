"""Trash source originals after a verified migration.

v0.5-b: reads a manifest emitted by :func:`execute_migration` and, only
after :func:`verify_migration` has stamped every ``state="done"`` entry
with ``verified=True`` + a ``verified_ts``, sends the corresponding source
originals to the cloud provider's trash.  Dry-run by default; ``--commit``
required to fire the actual ``move_to_trash`` calls.

Refuse conditions (each raises :class:`CleanupError` BEFORE any trash call):

- Any ``state="done"`` entry has ``verified=False``.
- Any ``state="done"`` entry has ``verified_ts=None``.

Both point the user at ``dc migrate verify`` so the invariant "cleanup
refuses without verify" cannot be bypassed by accident.

Per-entry error tolerance: :class:`SourceNotFoundError` /
:class:`SourcePermissionError` are logged and counted, the run continues.
:class:`SourceAuthError` / :class:`SourceRateLimitError` abort the run so a
bad token cannot silently miss most of the batch.
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
)

log = logging.getLogger(__name__)


class CleanupError(RuntimeError):
    """Raised when cleanup cannot safely proceed."""


@dataclass
class CleanupResult:
    """Return shape for :func:`cleanup_source_after_migration`."""

    planned: int = 0
    trashed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    committed: bool = False
    manifest_path: Path | None = None


def cleanup_source_after_migration(
    manifest_path: Path,
    *,
    commit: bool = False,
    sources_by_id: dict[str, Source],
) -> CleanupResult:
    """Trash every done+verified entry's source original.  Dry-run unless ``commit``.

    Every entry with ``state="done"`` is inspected up-front for verified
    status — a single unverified entry raises :class:`CleanupError` and no
    trash call fires.  This is the load-bearing invariant "cleanup refuses
    without verify".
    """
    manifest = _load_manifest(manifest_path)
    result = CleanupResult(manifest_path=manifest_path)

    # Refuse up-front on any unverified done entry — before any per-entry
    # dispatch loop, so a partial run cannot bypass the check by processing
    # verified entries first and blowing up on the unverified tail.
    for entry in manifest.entries:
        if entry.state != "done":
            continue
        if not entry.verified:
            raise CleanupError(
                f"Refusing cleanup: entry {entry.source_path!r} has "
                "state='done' but verified=False. Run `dc migrate verify "
                f"{manifest_path}` first."
            )
        if entry.verified_ts is None:
            raise CleanupError(
                f"Refusing cleanup: entry {entry.source_path!r} has "
                "state='done' but no verified_ts stamp. Run "
                f"`dc migrate verify {manifest_path}` first."
            )
        if entry.cleanup_done:
            # Already trashed on a prior cleanup — count as already-done and
            # skip during the loop below (idempotent).
            continue
        result.planned += 1

    if not commit:
        return result

    src_source = sources_by_id.get(manifest.plan_source_id)
    if src_source is None:
        raise CleanupError(
            f"Refusing cleanup: source {manifest.plan_source_id!r} not in "
            f"sources_by_id map ({sorted(sources_by_id.keys())})."
        )
    if getattr(src_source, "is_read_only_scan", False):
        raise CleanupError(
            f"Refusing cleanup: source {manifest.plan_source_id!r} was "
            "constructed with is_read_only_scan=True.  Construct with False "
            "for `dc migrate cleanup`."
        )

    for i, entry in enumerate(manifest.entries):
        if entry.state != "done":
            continue
        if entry.cleanup_done:
            continue

        # Rebuild the source-side record — Alt-C: path passed through, never
        # ``.resolve()``d.  The cloud identity trio (source_id + cloud_file_id
        # + etag) is what drives ``move_to_trash``.
        record = FileRecord(
            path=Path(entry.source_path),
            size=int(entry.source_size),
            mtime=0.0,
            inode=0,
            dev=0,
            nlink=1,
            source_id=entry.source_id,
            cloud_file_id=entry.source_file_id,
            etag=entry.source_etag,
        )
        try:
            trashed = src_source.move_to_trash(record)
        except (SourceAuthError, SourceRateLimitError) as exc:
            _flush_manifest(manifest, manifest_path)
            raise CleanupError(
                f"Aborted cleanup: {type(exc).__name__} on "
                f"{entry.source_path}: {exc}. "
                f"Manifest at {manifest_path} reflects reality "
                f"({result.trashed} source original(s) trashed so far)."
            ) from exc
        except (SourceNotFoundError, SourcePermissionError) as exc:
            log.warning(
                "Cleanup skipped for %s: %s", entry.source_path, exc
            )
            manifest.entries[i] = entry.model_copy(
                update={"error_message": f"cleanup: {exc}"}
            )
            result.skipped += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue
        except SourceError as exc:
            log.warning(
                "Cleanup failed for %s: %s", entry.source_path, exc
            )
            manifest.entries[i] = entry.model_copy(
                update={"error_message": f"cleanup: {exc}"}
            )
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue

        manifest.entries[i] = entry.model_copy(
            update={
                "cleanup_done": True,
                "source_cloud_trash_id": trashed.cloud_trash_id
                or trashed.cloud_file_id,
                "error_message": None,
            }
        )
        result.trashed += 1
        _flush_manifest(manifest, manifest_path)

    result.committed = True
    return result
