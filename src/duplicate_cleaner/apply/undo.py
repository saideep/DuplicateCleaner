"""Restore files from an apply-run manifest back to their original locations."""
from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.paths import (
    resolve_for_check,
    trash_dir_for,
    validate_not_excluded,
    validate_scan_root_candidate,
)

log = logging.getLogger(__name__)

# 1 MiB chunks for content hashing. Mirrors ``hash.pipeline.FULL_READ_BLOCK``
# — we do not import from there to keep undo independent of the pipeline.
_HASH_READ_BLOCK = 1 << 20


class UndoError(RuntimeError):
    """Raised when a restore cannot proceed safely."""


TrashResolver = Callable[[Path], Path]


def _hash_file(path: Path) -> str:
    """BLAKE3 hex digest of the file — used to disambiguate trash candidates."""
    h = blake3.blake3()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_READ_BLOCK)
            if not chunk:
                break
            h.update(chunk)
    return str(h.hexdigest())


def _locate_by_basename_and_size(
    trash: Path,
    name: str,
    size: int,
    *,
    expected_hash: str | None = None,
) -> Path | None:
    """Scan a trash directory for the trashed file corresponding to ``name``.

    Used when the manifest's ``trashed_at_path`` is missing or stale.

    G4 fixes:

    1. macOS ``send2trash`` renames on same-basename collision — ``foo.txt``
       already in Trash + new ``foo.txt`` → new file lands as ``foo 2.txt``
       (or ``foo N.txt``). Match ``^{stem}( \\d+)?{suffix}$``.
    2. If the manifest carries a ``hash`` for the entry, verify each
       (basename, size) match against it — a file with matching basename+size
       but different bytes must never be accepted as the restore source.
    3. If more than one candidate still matches after hashing: return ``None``
       and let the caller emit a clear ambiguity error listing them.
    """
    if not trash.exists():
        return None
    try:
        candidates = list(trash.iterdir())
    except OSError:
        return None

    stem = Path(name).stem
    suffix = Path(name).suffix
    # Match "foo.txt", "foo 1.txt", "foo 2.txt", ...
    name_re = re.compile(
        rf"^{re.escape(stem)}( \d+)?{re.escape(suffix)}$"
    )

    matches: list[Path] = []
    for candidate in candidates:
        if not name_re.match(candidate.name):
            continue
        try:
            if not candidate.is_file() or candidate.stat().st_size != size:
                continue
        except OSError:
            continue
        matches.append(candidate)

    if not matches:
        return None

    if expected_hash:
        verified = [m for m in matches if _hash_safely(m) == expected_hash]
        if not verified:
            return None
        if len(verified) > 1:
            raise UndoError(
                f"Multiple hash-identical trash candidates for {name}: "
                f"{[str(v) for v in verified]}"
            )
        return verified[0]

    # No hash to check against.
    if len(matches) > 1:
        raise UndoError(
            f"Ambiguous restore for {name} (no hash to disambiguate): "
            f"{[str(m) for m in matches]}"
        )
    return matches[0]


def _hash_safely(path: Path) -> str | None:
    try:
        return _hash_file(path)
    except OSError as exc:
        log.warning("Could not hash trash candidate %s: %s", path, exc)
        return None


def restore_from_manifest(
    manifest_path: Path,
    *,
    trash_dir_resolver: TrashResolver | None = None,
) -> dict[str, Any]:
    """Move each manifest entry from its trashed_at_path back to original_path.

    Safety rails:

    * Every entry's ``original_path`` is validated against EXCLUDED_ROOTS
      BEFORE any filesystem write. A poisoned or altered manifest cannot
      trick undo into restoring into ``/System``, ``~/Library``, etc.
    * When the manifest lacks a ``trashed_at_path`` (crash mid-apply), we
      pick the correct Trash directory for the original — ``~/.Trash`` for
      the boot volume, ``/Volumes/<VOL>/.Trashes/<uid>/`` for externals —
      and locate the trashed file by (basename, size).
    """
    resolve_trash = trash_dir_resolver or trash_dir_for

    data = json.loads(manifest_path.read_text())
    entries: list[dict[str, Any]] = data.get("entries", [])

    # G1: some manifests may carry a ``roots`` array (added in v0.1.1 for
    # future report/manifest linkage). If it is present, validate each entry
    # with the same helper used by ``dc scan`` and ``apply``. A poisoned
    # manifest with ``roots=["/"]`` must be rejected before any per-entry
    # write.
    manifest_roots = data.get("roots")
    if isinstance(manifest_roots, list) and manifest_roots:
        for r in manifest_roots:
            try:
                validate_scan_root_candidate(Path(str(r)))
            except ValueError as e:
                raise UndoError(
                    f"Refuse to restore: manifest.roots entry rejected — {e}"
                ) from e

    restored = 0
    errors: list[str] = []
    for entry in entries:
        original = Path(entry["original_path"])
        size = int(entry.get("size", 0))
        trashed_at_raw = entry.get("trashed_at_path")
        expected_hash = entry.get("hash")
        if not isinstance(expected_hash, str):
            expected_hash = None

        # F12: validate original_path against EXCLUDED_ROOTS. A tampered
        # manifest must never coerce undo into restoring inside excluded
        # roots (~/Library, /System, /private, ...).
        resolved_original = resolve_for_check(original)
        try:
            validate_not_excluded(resolved_original)
        except ValueError as e:
            errors.append(
                f"Refuse to restore into excluded location {original}: {e}"
            )
            continue

        src: Path | None = None
        if isinstance(trashed_at_raw, str):
            candidate = Path(trashed_at_raw)
            if candidate.exists():
                # trashed_at_path was recorded at trash time — trust it.
                # Hash verification only runs on the fallback path where
                # we're picking a candidate out of the Trash by basename,
                # not on an explicit source recorded by the mover itself.
                src = candidate
        if src is None:
            trash = resolve_trash(original)
            try:
                src = _locate_by_basename_and_size(
                    trash,
                    original.name,
                    size,
                    expected_hash=expected_hash,
                )
            except UndoError as e:
                errors.append(str(e))
                continue

        if src is None:
            errors.append(f"Cannot locate trashed file for {original}")
            continue
        if original.exists():
            errors.append(
                f"Original already exists, refusing to overwrite: {original}"
            )
            continue

        original.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(original))
        except OSError as exc:
            errors.append(f"Restore failed for {original}: {exc}")
            continue
        restored += 1

    return {"restored": restored, "errors": errors, "total": len(entries)}
