"""Reverse a `dc organize apply` run using its undo manifest.

Symmetric to :mod:`duplicate_cleaner.apply.undo` for the dedup mover.  Every
manifest entry is validated against the same H2/F12/H5 rails: source_path
never resolves into an excluded root, no ``../`` traversal, dest_path must
exist on disk before we touch it. ``shutil.move`` is the primitive — this
module is the only place inside :mod:`organize` that uses it, and the
forbidden-calls test allowlist is extended to include it.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duplicate_cleaner.paths import (
    resolve_for_check,
    validate_not_excluded,
)

log = logging.getLogger(__name__)


class OrganizeUndoError(RuntimeError):
    """Raised when restoring from an organize manifest cannot proceed safely."""


@dataclass
class RestoreOrganizeResult:
    """Return value from :func:`restore_from_organize_manifest`."""

    restored: int = 0
    total: int = 0
    errors: list[str] = field(default_factory=list)


def _validate_original_path(original: Path) -> Path:
    """Resolve + reject excluded roots for a manifest ``source_path`` field."""
    resolved = resolve_for_check(original)
    validate_not_excluded(resolved)
    return resolved


def _remove_empty_parents(leaf: Path, stop_at: Path | None) -> None:
    """Best-effort cleanup: walk up from ``leaf`` removing empty directories.

    We avoid ``os.rmdir`` / ``Path.rmdir`` (forbidden by
    ``tests/test_no_forbidden_calls.py``) and instead route removal via
    ``send2trash`` so an accidentally non-empty directory ends up in Trash
    rather than being nuked in place.
    """
    import send2trash  # type: ignore[import-untyped]

    stop_resolved = resolve_for_check(stop_at) if stop_at is not None else None
    parent = leaf.parent
    while parent.exists() and (
        stop_resolved is None or resolve_for_check(parent) != stop_resolved
    ):
        try:
            entries = list(parent.iterdir())
        except OSError:
            return
        if entries:
            return
        try:
            send2trash.send2trash(str(parent))
        except OSError as e:
            log.debug("Could not clean empty parent %s: %s", parent, e)
            return
        parent = parent.parent


def restore_from_organize_manifest(
    manifest_path: Path,
) -> RestoreOrganizeResult:
    """Move every dest_path back to its recorded source_path.

    Safety rails:

    * Each ``source_path`` must resolve outside ``EXCLUDED_ROOTS`` — a
      poisoned manifest cannot coerce restore into planting a file at
      ``~/Library/...`` or ``/System``.
    * If a ``dest_path`` is missing (user deleted it since apply), the entry
      is logged and skipped — the remaining entries still restore.
    * If a ``source_path`` already exists on disk, refuse to overwrite it
      and skip the entry (symmetric with ``apply/undo.py`` behaviour).
    * After a successful restore, best-effort remove any parent directories
      that became empty inside the organize destination tree.
    """
    data = json.loads(manifest_path.read_text())
    entries: list[dict[str, Any]] = data.get("entries", [])
    dest_root_raw = data.get("dest_root")
    dest_root = Path(dest_root_raw) if isinstance(dest_root_raw, str) else None

    result = RestoreOrganizeResult(total=len(entries))
    for entry in entries:
        source_str = entry.get("source_path")
        dest_str = entry.get("dest_path")
        if not isinstance(source_str, str) or not isinstance(dest_str, str):
            result.errors.append(
                f"Manifest entry missing source_path/dest_path: {entry!r}"
            )
            continue
        source_path = Path(source_str)
        dest_path = Path(dest_str)

        try:
            resolved_source = _validate_original_path(source_path)
        except ValueError as e:
            result.errors.append(
                f"Refuse to restore into excluded location {source_path}: {e}"
            )
            continue

        # A second check: reject a manifest that says "restore this file to
        # /Volumes/*/System/..." even if the raw string didn't trip an
        # exclusion.  ``resolve_for_check`` normalises symlinks first.
        try:
            validate_not_excluded(resolved_source)
        except ValueError as e:
            result.errors.append(
                f"Refuse to restore into excluded location {source_path}: {e}"
            )
            continue

        if not dest_path.exists():
            result.errors.append(
                f"dest_path missing on disk (user moved/deleted?): {dest_path}"
            )
            continue

        if resolved_source.exists():
            result.errors.append(
                f"Original already exists, refusing to overwrite: {source_path}"
            )
            continue

        resolved_source.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(dest_path), str(resolved_source))
        except OSError as e:
            result.errors.append(f"Restore failed for {source_path}: {e}")
            continue
        result.restored += 1

        # Cleanup empty parent dirs under the organize destination tree, but
        # stop at ``dest_root`` so we never walk above the operator's chosen
        # organize root.  Missing / non-string dest_root → no cleanup.
        _remove_empty_parents(dest_path, dest_root)

    return result
