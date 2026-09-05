"""Restore files from an apply-run manifest back to their original locations."""
from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.auth.accounts import AccountsRegistry
from duplicate_cleaner.compare.archive import ARCHIVE_SEP
from duplicate_cleaner.paths import (
    is_within,
    known_trash_dirs,
    resolve_for_check,
    trash_dir_for,
    validate_cloud_manifest_entry,
    validate_not_excluded,
    validate_scan_root_candidate,
)
from duplicate_cleaner.sources.base import (
    SourceAuthError,
    SourceError,
    SourceNotFoundError,
    SourceRateLimitError,
    TrashedLocation,
)

if TYPE_CHECKING:
    from duplicate_cleaner.sources.base import Source

log = logging.getLogger(__name__)

# 1 MiB chunks for content hashing. Mirrors ``hash.pipeline.FULL_READ_BLOCK``
# — we do not import from there to keep undo independent of the pipeline.
_HASH_READ_BLOCK = 1 << 20


class UndoError(RuntimeError):
    """Raised when a restore cannot proceed safely."""


TrashResolver = Callable[[Path], Path]


def validate_restore_paths(
    original_path: Path,
    trashed_at_path: Path,
    *,
    allowed_trash_dirs: list[Path] | None = None,
) -> None:
    """Enforce H2/F12/H5 rails on a single (original, trashed_at) pair.

    Shared between :func:`restore_from_manifest` (batch undo) and
    :func:`local_restore` (per-entry Source.restore_from_trash).  Callers of
    the low-level source method now go through the same rails as the manifest
    driver, so a poisoned ``TrashedLocation`` cannot coerce ``shutil.move``
    into relocating ``~/.ssh/id_rsa`` (H2), restoring into an excluded root
    like ``/System`` (F12), or materialising an archive-member path (H5).
    """
    if ARCHIVE_SEP in str(original_path):
        raise UndoError(
            f"Refuse to restore archive-member path (contains {ARCHIVE_SEP!r}): "
            f"{original_path}"
        )
    resolved_original = resolve_for_check(original_path)
    try:
        validate_not_excluded(resolved_original)
    except ValueError as e:
        raise UndoError(
            f"Refuse to restore into excluded location {original_path}: {e}"
        ) from e
    trash_roots: list[Path] = (
        [resolve_for_check(p) for p in allowed_trash_dirs]
        if allowed_trash_dirs is not None
        else known_trash_dirs()
    )
    resolved_src = resolve_for_check(trashed_at_path)
    if not any(is_within(resolved_src, root) for root in trash_roots):
        raise UndoError(
            f"Refuse to restore: trashed_at_path {trashed_at_path} is not "
            f"inside a known Trash directory "
            f"({[str(r) for r in trash_roots]})"
        )


def local_restore(
    src: Path,
    dst: Path,
    *,
    allowed_trash_dirs: list[Path] | None = None,
) -> None:
    """Move ``src`` back to ``dst`` — keeps ``shutil.move`` centralised here.

    ``LocalFileSystemSource.restore_from_trash`` calls this so the
    forbidden-calls whitelist can continue to name exactly one file
    (``apply/undo.py``) as the legitimate holder of ``shutil.move``.  Every
    call flows through :func:`validate_restore_paths` — no caller can skip
    the H2/F12/H5 rails even by wiring the source's method directly.
    """
    validate_restore_paths(dst, src, allowed_trash_dirs=allowed_trash_dirs)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


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
    registry: AccountsRegistry | None = None,
    sources: dict[str, Source] | None = None,
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

    v0.2 sub-phase 5d — cloud dispatch: ``sources`` maps ``source_id`` (e.g.
    ``"gdrive:personal"``) to a concrete :class:`Source` whose
    ``restore_from_trash`` method drives the provider-side un-trash.  For each
    cloud manifest row we validate the entry shape (audit pass-10 finding #1
    — ``cloud_trash_id`` MUST NOT be ``None`` on an entry produced by a
    successful mover run; a null value means the mover logged-and-continued
    on a ``SourceNotFoundError`` at trash time and there is nothing to
    restore).  Semantics on cloud failures:

    * :class:`SourceNotFoundError` — recycle bin emptied by user via web UI.
      Log + increment ``errors``, continue.  (Personal restore is
      ``notSupported`` anyway — user must use the web UI.)
    * :class:`SourceAuthError` / :class:`SourceRateLimitError` — abort the
      whole undo run.  Continuing under a bad token silently misses many
      restores; better to fail loud with one action item.
    * Other :class:`SourceError` — logged per-entry, continue.

    ``sources=None`` means local-only mode: a manifest that carries any
    cloud entry surfaces those entries as per-entry ``errors`` (no run
    abort) so a v0.1.1 caller of ``restore_from_manifest`` continues to
    work on a pure-local manifest untouched.
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
    # ``foo.zip::etc/passwd``-shaped directories.  Scoped to LOCAL entries
    # only: a cloud original_path is opaque display text (e.g.
    # ``gdrive:x://foo``) and does not represent a real filesystem target.
    offending = [
        entry.get("original_path")
        for entry in entries
        if entry.get("source_id", "local") == "local"
        and isinstance(entry.get("original_path"), str)
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

    # A registry read is required for cloud dispatch.  Construction is cheap
    # (no disk I/O until .load()) so we build it lazily — every v0.1.1 local
    # manifest continues to restore without a real accounts.toml present.
    reg: AccountsRegistry = registry if registry is not None else AccountsRegistry()

    restored_local = 0
    restored_cloud = 0
    skipped_cloud = 0
    errors: list[str] = []
    for entry in entries:
        # v0.2 sub-phase 5b: dispatch by source_id BEFORE any local-path
        # validation.  Cloud entries never touch resolve_for_check or the
        # trash containment checks — Alt-C treats their ``original_path``
        # as opaque display data.
        sid = entry.get("source_id", "local")
        if sid != "local":
            try:
                validate_cloud_manifest_entry(entry, reg)
            except ValueError as e:
                errors.append(
                    f"Refuse to restore cloud entry "
                    f"{entry.get('original_path')!r}: {e}"
                )
                continue
            # Audit pass-10 finding #1: entries with cloud_trash_id=None
            # are aborted-apply artifacts (the mover logged-and-continued
            # on a SourceNotFoundError from move_to_trash because the file
            # was already gone at trash time).  Restoring one would silently
            # un-trash a file another client had trashed — reverse user
            # intent.  REJECT loudly.
            cloud_trash_id = entry.get("cloud_trash_id")
            if not isinstance(cloud_trash_id, str) or not cloud_trash_id:
                errors.append(
                    f"Refuse to restore cloud entry "
                    f"{entry.get('original_path')!r}: cloud entry from an "
                    "aborted apply — nothing to restore (file was already "
                    "gone at trash time)."
                )
                continue
            # Local-only mode (v0.1.1 caller shape): surface as per-entry
            # error, do not abort — the local part of the manifest still
            # restores.
            if sources is None:
                errors.append(
                    f"Refuse to restore cloud entry "
                    f"{entry.get('original_path')!r}: no sources map "
                    "provided (local-only undo mode)."
                )
                continue
            cloud_src = sources.get(sid)
            if cloud_src is None:
                errors.append(
                    f"Refuse to restore cloud entry "
                    f"{entry.get('original_path')!r}: source_id {sid!r} not "
                    f"present in sources map ({sorted(sources.keys())}). "
                    f"Run `dc auth add {sid.split(':', 1)[0]} <label>` for "
                    "the account and retry."
                )
                continue
            cloud_file_id = entry.get("cloud_file_id")
            if not isinstance(cloud_file_id, str):
                # Belt-and-braces: validate_cloud_manifest_entry above
                # already rejects a missing / non-string cloud_file_id.
                errors.append(
                    f"Refuse to restore cloud entry "
                    f"{entry.get('original_path')!r}: cloud_file_id absent."
                )
                continue
            original_path_raw = entry.get("original_path")
            original_path_str = (
                original_path_raw if isinstance(original_path_raw, str) else ""
            )
            loc = TrashedLocation(
                source_id=sid,
                original_path=original_path_str,
                cloud_file_id=cloud_file_id,
                cloud_trash_id=cloud_trash_id,
            )
            try:
                cloud_src.restore_from_trash(loc)
            except SourceNotFoundError as e:
                # Web-UI empty-Trash / permanent-delete — nothing to un-trash.
                # OneDrive Personal restore is 501/notSupported so this branch
                # also catches the "you must use the web UI" case there.
                log.warning(
                    "Cloud restore skipped for %s: %s",
                    original_path_str,
                    e,
                )
                errors.append(
                    f"{original_path_str}: cloud trash was emptied — "
                    "unable to restore.  Check the provider's web UI."
                )
                skipped_cloud += 1
                continue
            except (SourceAuthError, SourceRateLimitError) as e:
                # Abort — a bad token / hard rate-limit would silently miss
                # every following entry.  Surface loudly with one action item.
                raise UndoError(
                    f"Aborted mid-undo: {type(e).__name__} restoring "
                    f"{original_path_str}: {e}.  Fix the underlying issue "
                    "(run `dc auth add --force` for auth) and retry."
                ) from e
            except SourceError as e:
                # Provider-side failure — surface but keep restoring.
                errors.append(f"{original_path_str}: {e}")
                continue
            restored_cloud += 1
            continue

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
        restored_local += 1

    # v0.2 sub-phase 5d: split ``restored`` into per-family counters + a
    # ``skipped_cloud`` counter for source-not-found / permission failures.
    # ``restored`` stays for backwards-compat with v0.1.1 callers (CLI
    # summary + existing tests) as the sum of local + cloud restores.
    # ``cloud_deferred`` is preserved as 0 in 5d — cloud entries either
    # restore (counted in ``restored_cloud``), skip (``skipped_cloud``), or
    # error out (in ``errors``).  A v0.1.1-shaped local-only manifest still
    # returns ``cloud_deferred=0`` and ``restored=restored_local``.
    return {
        "restored": restored_local + restored_cloud,
        "restored_local": restored_local,
        "restored_cloud": restored_cloud,
        "skipped_cloud": skipped_cloud,
        "errors": errors,
        "total": len(entries),
        "cloud_deferred": 0,
    }
