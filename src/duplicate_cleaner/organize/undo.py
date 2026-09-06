"""Reverse a `dc organize apply` run using its undo manifest.

Symmetric to :mod:`duplicate_cleaner.apply.undo` for the dedup mover.  Every
manifest entry is validated against the same H2/F12/H5 rails: source_path
never resolves into an excluded root, no ``../`` traversal, dest_path must
exist on disk before we touch it. ``shutil.move`` is the primitive — this
module is the only place inside :mod:`organize` that uses it, and the
forbidden-calls test allowlist is extended to include it.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from duplicate_cleaner.organize.plan import OrganizeManifest
from duplicate_cleaner.paths import (
    resolve_for_check,
    validate_not_excluded,
)

log = logging.getLogger(__name__)

# Audit pass 13 finding: cap the empty-parent-cleanup walker at 8 hops
# even when the manifest's dest_root check is intact. Belt-and-braces
# against a hand-edited manifest whose dest_root points somewhere the
# walker will never reach (a sibling volume, a moved directory).
_EMPTY_PARENT_MAX_HOPS = 8


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

    Audit pass 13 finding: bound the walker at ``_EMPTY_PARENT_MAX_HOPS``
    hops even when ``stop_at`` looks correct.  A hand-edited manifest
    whose ``dest_root`` points at a moved / sibling-volume directory
    could otherwise let the walker climb until ``parent.exists()`` is
    False — which is a lot of send2trash calls.  Every real dedup /
    organize tree is well within 8 levels of ``dest_root``.
    """
    import send2trash  # type: ignore[import-untyped]

    stop_resolved = resolve_for_check(stop_at) if stop_at is not None else None
    parent = leaf.parent
    for _hop in range(_EMPTY_PARENT_MAX_HOPS):
        if not parent.exists():
            return
        if stop_resolved is not None and resolve_for_check(parent) == stop_resolved:
            return
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

    * Manifest loaded via Pydantic ``OrganizeManifest.model_validate_json``
      — a hand-edited manifest with a wrong-typed column (e.g. ``size="banana"``)
      raises :class:`OrganizeUndoError` before any move fires (audit
      pass 13 finding).
    * Each ``source_path`` must resolve outside ``EXCLUDED_ROOTS`` — a
      poisoned manifest cannot coerce restore into planting a file at
      ``~/Library/...`` or ``/System``.
    * If a ``dest_path`` is missing (user deleted it since apply), the entry
      is logged and skipped — the remaining entries still restore.
    * If a ``source_path`` already exists on disk, refuse to overwrite it
      and skip the entry (symmetric with ``apply/undo.py`` behaviour).
    * After a successful restore, best-effort remove any parent directories
      that became empty inside the organize destination tree, bounded at
      :data:`_EMPTY_PARENT_MAX_HOPS` hops.
    """
    raw = manifest_path.read_text()
    try:
        manifest = OrganizeManifest.model_validate_json(raw)
    except ValidationError as e:
        raise OrganizeUndoError(
            f"Refusing to restore: manifest at {manifest_path} failed schema "
            f"validation. This usually means the file was hand-edited or "
            f"produced by a different tool version. Details: {e}"
        ) from e

    dest_root = manifest.dest_root

    result = RestoreOrganizeResult(total=len(manifest.entries))
    for entry in manifest.entries:
        source_path = entry.source_path
        dest_path = entry.dest_path

        try:
            resolved_source = _validate_original_path(source_path)
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
        # organize root.  Bounded at _EMPTY_PARENT_MAX_HOPS.
        _remove_empty_parents(dest_path, dest_root)

    return result
