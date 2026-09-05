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

from duplicate_cleaner.compare.archive import ARCHIVE_SEP
from duplicate_cleaner.paths import (
    is_within,
    known_trash_dirs,
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
    allowed_trash_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Move each manifest entry from its trashed_at_path back to original_path.

    Safety rails:

    * Every entry's ``original_path`` is validated against EXCLUDED_ROOTS
      BEFORE any filesystem write. A poisoned or altered manifest cannot
      trick undo into restoring into ``/System``, ``~/Library``, etc.
    * H5: every entry's ``original_path`` is rejected if it contains the
      archive-member separator (``::``). The mover already refuses to trash
      archive members individually — undo must be symmetric.
    * H2: every source path (``trashed_at_path`` or a candidate from the
      basename fallback) must resolve into one of the platform's known
      Trash directories (``~/.Trash``, ``/Volumes/*/.Trashes/<uid>/``).
      Without this check, a poisoned manifest could point ``trashed_at_path``
      at ``~/.ssh/id_rsa`` and undo would ``shutil.move`` that file into a
      scan root.
    * When the manifest lacks a ``trashed_at_path`` (crash mid-apply), we
      pick the correct Trash directory for the original — ``~/.Trash`` for
      the boot volume, ``/Volumes/<VOL>/.Trashes/<uid>/`` for externals —
      and locate the trashed file by (basename, size).

    ``allowed_trash_dirs`` overrides the production Trash-directory list —
    tests inject a temp-directory Trash so end-to-end undo flows don't
    require a real ``~/.Trash`` write.
    """
    resolve_trash = trash_dir_resolver or trash_dir_for
    trash_roots: list[Path] = (
        [resolve_for_check(p) for p in allowed_trash_dirs]
        if allowed_trash_dirs is not None
        else known_trash_dirs()
    )

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

    # H5: reject entries whose ``original_path`` includes the archive-member
    # separator ``::`` — the mover would never trash an archive member
    # individually, so a manifest that claims one must be malformed or
    # poisoned. Raising up-front prevents restore-time creation of
    # ``foo.zip::etc/passwd``-shaped directories.
    offending = [
        entry.get("original_path")
        for entry in entries
        if isinstance(entry.get("original_path"), str)
        and ARCHIVE_SEP in entry["original_path"]
    ]
    if offending:
        raise UndoError(
            "Refuse to restore: manifest contains archive-member original_path "
            f"entries (contain {ARCHIVE_SEP!r}): {offending}"
        )

    def _src_is_in_trash(src: Path) -> bool:
        """Contain check — src.resolve() must sit inside a known Trash dir."""
        resolved = resolve_for_check(src)
        return any(is_within(resolved, root) for root in trash_roots)

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
                # H2: still require it to resolve inside a known Trash dir.
                # A poisoned manifest may otherwise coerce undo into moving
                # arbitrary user files (~/.ssh/id_rsa, ...).
                if not _src_is_in_trash(candidate):
                    errors.append(
                        f"Refuse to restore: trashed_at_path {candidate} "
                        f"is not inside a known Trash directory "
                        f"({[str(r) for r in trash_roots]})"
                    )
                    continue
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
            # H2: apply the same containment check to fallback candidates.
            # Even if ``resolve_trash`` was overridden, the located file
            # must physically resolve into a whitelisted Trash root.
            if src is not None and not _src_is_in_trash(src):
                errors.append(
                    f"Refuse to restore: located source {src} is not "
                    f"inside a known Trash directory "
                    f"({[str(r) for r in trash_roots]})"
                )
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
