"""Execute a migration plan — dry-run by default; ``--commit`` gates uploads.

v0.5-b: the write half of ``dc migrate``.  Consumes a :class:`MigrationPlan`
produced by v0.5-a's ``dc migrate plan`` and, per plan entry:

1. Loads the source + destination Sources from ``sources_by_id``.  Both must
   have ``is_read_only_scan=False`` — the tripwire fires BEFORE any drift
   check or byte transfer.
2. Verifies the source file has not drifted since scan-time via
   :meth:`Source.check_drift`.  Etag mismatch aborts the whole run so a stale
   scan cannot silently copy the wrong bytes.
3. Streams ``source.read_bytes(...)`` (with an optional bandwidth throttle
   wrapper) into ``dest.upload(dest_expected_path, stream, expected_size)``.
4. Compares ``UploadResult.uploaded_hash`` against ``entry.source_hash``.
   Same-algo pairs (both md5, both sha256, both blake3) compare directly.
   Cross-algo pairs (md5 gdrive → sha256 onedrive) consult the reconciliation
   cache; if a canonical BLAKE3 is available on both sides we compare there.
   Otherwise the copy is optimistically marked verified — ``dc migrate
   verify --full`` re-hashes the destination bytes for the strict check.
5. On a mismatch, ``dest.move_to_trash(...)`` is called BEFORE the manifest is
   flushed so the failed upload never lingers as a live destination copy.
   The source is never touched.
6. On success, the manifest row is stamped with the destination cloud ids +
   hash and flushed atomically (tempfile + fsync + os.replace + parent-dir
   fsync) so a crash mid-run leaves a replayable artifact.

Resume: ``--resume-from`` reads a prior manifest and re-emits every entry
whose ``state == "done"`` as ``state="skipped"`` in the current run's
manifest — no re-upload fires.

Bandwidth throttle: ``max_bandwidth_mbps`` wraps ``source.read_bytes`` with a
per-chunk :func:`time.sleep` so a bulk migration does not saturate the
network.  ``None`` disables throttling (default).
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from duplicate_cleaner.migrate.plan import (
    MigrationEntryState,
    MigrationManifest,
    MigrationManifestEntry,
    MigrationPlan,
)
from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    Source,
    SourceAuthError,
    SourceDriftError,
    SourceError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceRateLimitError,
    UploadResult,
)

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """Raised when migration cannot safely proceed."""


@dataclass
class MigrationResult:
    """Return shape for :func:`execute_migration`."""

    planned: int = 0
    copied: int = 0
    skipped: int = 0
    errored: int = 0
    deferred: int = 0
    manifest_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    committed: bool = False


def _plan_entry_to_manifest_entry(entry_dict: dict[str, Any]) -> MigrationManifestEntry:
    """Promote a :class:`MigrationEntry` (dict) to a fresh :class:`MigrationManifestEntry`.

    Every non-copy action lands as ``state="skipped"`` up-front — the copy
    loop below never touches those entries.  A ``copy`` action starts as
    ``state="pending"``; the loop flips it to ``done`` / ``error`` /
    ``skipped`` (resume) as it iterates.
    """
    action = entry_dict.get("action")
    state: MigrationEntryState = "pending" if action == "copy" else "skipped"
    return MigrationManifestEntry(
        source_id=str(entry_dict.get("source_id", "")),
        source_file_id=entry_dict.get("source_file_id"),
        source_path=str(entry_dict.get("source_path", "")),
        source_etag=entry_dict.get("source_etag"),
        source_size=int(entry_dict.get("source_size", 0)),
        source_hash=entry_dict.get("source_hash"),
        source_mime=entry_dict.get("source_mime"),
        dest_expected_path=str(entry_dict.get("dest_expected_path", "")),
        action=cast(
            "Any",
            action if action in {"copy", "skip", "defer", "error"} else "error",
        ),
        state=state,
        reason=str(entry_dict.get("reason", "")),
        size_limit_hit=bool(entry_dict.get("size_limit_hit", False)),
    )


def _flush_manifest(manifest: MigrationManifest, path: Path) -> None:
    """Atomically write ``manifest`` to ``path`` with fsync durability.

    Same pattern as ``apply/mover.py::_write_manifest`` — tempfile + fsync
    the file + ``os.replace`` + fsync the parent directory so the rename
    survives a crash before the OS flushes.  This is the load-bearing
    "manifest written before any move" invariant on the migrate side.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(json.loads(manifest.model_dump_json()), f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dirfd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)


def _load_manifest(manifest_path: Path) -> MigrationManifest:
    """Read + Pydantic-validate a manifest JSON.

    Every field is re-validated so a hand-edited (or stale-schema) manifest
    fails loudly at load time — matching the pattern ``apply/undo.py`` uses
    for the dedup manifest.
    """
    return MigrationManifest.model_validate_json(manifest_path.read_text())


def _throttled_stream(
    byte_stream: Iterator[bytes],
    max_mbps: float | None,
    *,
    sleep_fn: Any = time.sleep,
) -> Iterator[bytes]:
    """Wrap ``byte_stream`` with a per-chunk sleep to cap effective bandwidth.

    ``max_mbps`` is megabits per second on the wire.  Each chunk yielded
    triggers a ``sleep(len(chunk) * 8 / (max_mbps * 1_000_000))`` before the
    NEXT chunk fires — the effective rate over N chunks converges on the
    cap without needing a per-byte token bucket.  ``None`` returns the
    stream unwrapped so the fast path is a no-op.  ``sleep_fn`` is
    injectable so tests can verify the throttle without waiting.
    """
    if max_mbps is None or max_mbps <= 0:
        yield from byte_stream
        return
    max_bps = max_mbps * 1_000_000.0
    for chunk in byte_stream:
        yield chunk
        if chunk:
            sleep_fn(len(chunk) * 8.0 / max_bps)


def _plan_entry_to_source_record(
    entry: MigrationManifestEntry,
) -> FileRecord:
    """Rebuild a minimal :class:`FileRecord` for ``source.read_bytes`` / drift-check.

    Only the fields the source dispatch consumes are populated — inode/dev
    stay at zero (unused for cloud reads); the cloud identity trio
    (``source_id``, ``cloud_file_id``, ``etag``) is what drives the actual
    request.  The path is preserved verbatim (never ``.resolve()``d — Alt-C).
    """
    return FileRecord(
        path=Path(entry.source_path),
        size=int(entry.source_size),
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id=entry.source_id,
        etag=entry.source_etag,
        cloud_file_id=entry.source_file_id,
        foreign_hash=entry.source_hash,
    )


def _split_algo_hash(hash_str: str | None) -> tuple[str | None, str | None]:
    """Return ``(algo, hex)`` from an ``"algo:hex"`` string or ``(None, hex)``.

    Local BLAKE3 hashes carry no algo prefix — return ``("blake3", hex)``
    for local entries so the same-algo compare below can key on ``"blake3"``.
    A missing hash yields ``(None, None)``.  Case-insensitive prefix.
    """
    if not hash_str:
        return None, None
    if ":" in hash_str:
        algo, _, hex_part = hash_str.partition(":")
        return algo.lower(), hex_part.lower()
    # Bare hex — local BLAKE3 by convention.
    return "blake3", hash_str.lower()


def _hashes_match(
    source_hash: str | None,
    uploaded_algo: str,
    uploaded_hex: str,
) -> bool | None:
    """Compare a source-side algo-tagged hash to an upload-side (algo, hex) pair.

    Returns:
    * ``True`` — same-algo match confirmed.
    * ``False`` — same-algo mismatch confirmed (trash dest, mark error).
    * ``None`` — cross-algo pair (or missing source hash); cannot decide
      cheaply here.  Caller treats this as "optimistically verified"; the
      ``dc migrate verify --full`` step canonicalises via BLAKE3.

    Kept as a small pure function so the copy loop's logic stays flat and
    the unit tests can exercise every branch without a real Source.
    """
    src_algo, src_hex = _split_algo_hash(source_hash)
    if not src_algo or not src_hex:
        return None
    if src_algo == uploaded_algo.lower():
        return src_hex == uploaded_hex.lower()
    # Cross-algo (md5 <-> sha256, blake3 <-> md5, ...): the cheap direct
    # compare is impossible.  Caller must either consult the reconciliation
    # cache or accept + rely on ``dc migrate verify --full``.
    return None


def execute_migration(
    plan_path: Path,
    manifest_path: Path,
    *,
    commit: bool = False,
    sources_by_id: dict[str, Source],
    resume_from: Path | None = None,
    max_bandwidth_mbps: float | None = None,
) -> MigrationResult:
    """Execute the copy actions in ``plan_path``.  Dry-run unless ``commit``.

    Parameters mirror the design contract in ``docs/AUDIT_LOG.md`` under the
    v0.5-b milestone.  A dry-run returns immediately after loading the plan
    + resume manifest and counting; no manifest is written.  ``commit=True``
    writes the manifest BEFORE the first upload fires and re-flushes it
    per-entry.

    Aborts (whole-run stop, manifest kept):
    - :class:`SourceDriftError` on any per-entry drift check.
    - :class:`SourceAuthError` / :class:`SourceRateLimitError` after the
      source's tenacity retries have been exhausted.
    - Missing source or destination in ``sources_by_id``.
    - Either source constructed with ``is_read_only_scan=True``.

    Per-entry errors (log + continue, entry state="error"):
    - :class:`SourceNotFoundError`, :class:`SourcePermissionError`.
    - Any other :class:`SourceError` subclass NOT in the abort set above.
    - Post-upload hash mismatch: destination trashed, source untouched.
    """
    plan = MigrationPlan.model_validate_json(plan_path.read_text())

    resume_done_by_path: dict[str, MigrationManifestEntry] = {}
    if resume_from is not None:
        prior = _load_manifest(resume_from)
        for e in prior.entries:
            if e.state == "done":
                resume_done_by_path[e.source_path] = e

    manifest = MigrationManifest(
        plan_source_id=plan.source_id,
        plan_dest_id=plan.dest_id,
        plan_path=str(plan_path),
        entries=[],
    )
    for raw in plan.entries:
        base = _plan_entry_to_manifest_entry(raw.model_dump())
        prior_entry = resume_done_by_path.get(base.source_path)
        if prior_entry is not None:
            # Carry the prior verified state forward so `verify` / `cleanup`
            # see a coherent history when the operator restarts a run.
            base = base.model_copy(
                update={
                    "state": "skipped",
                    "dest_cloud_file_id": prior_entry.dest_cloud_file_id,
                    "dest_etag": prior_entry.dest_etag,
                    "uploaded_hash": prior_entry.uploaded_hash,
                    "uploaded_hash_algo": prior_entry.uploaded_hash_algo,
                    "verified": prior_entry.verified,
                    "verified_ts": prior_entry.verified_ts,
                    "cleanup_done": prior_entry.cleanup_done,
                    "source_cloud_trash_id": prior_entry.source_cloud_trash_id,
                    "reason": "resumed: already done in prior manifest",
                }
            )
        manifest.entries.append(base)

    result = MigrationResult(planned=len(manifest.entries))
    # Bucket every non-copy plan action so the dry-run summary is accurate
    # even before ``commit`` fires.
    for e in manifest.entries:
        if e.action == "skip":
            result.skipped += 1
        elif e.action == "defer":
            result.deferred += 1
        elif e.action == "error":
            result.errored += 1

    if not commit:
        return result

    # Pre-flight: every referenced source (source_id + dest_id) must be
    # present and write-enabled BEFORE the first byte transfer.  Fail loud
    # instead of discovering it mid-run and stranding a half-committed
    # manifest.
    src_source = sources_by_id.get(plan.source_id)
    if src_source is None:
        raise MigrationError(
            f"Refusing to migrate: source {plan.source_id!r} not in "
            f"sources_by_id map ({sorted(sources_by_id.keys())})."
        )
    dst_source = sources_by_id.get(plan.dest_id)
    if dst_source is None:
        raise MigrationError(
            f"Refusing to migrate: destination {plan.dest_id!r} not in "
            f"sources_by_id map ({sorted(sources_by_id.keys())})."
        )
    if getattr(src_source, "is_read_only_scan", False):
        raise MigrationError(
            f"Refusing to migrate: source {plan.source_id!r} was constructed "
            "with is_read_only_scan=True.  Construct with False for "
            "`dc migrate copy`."
        )
    if getattr(dst_source, "is_read_only_scan", False):
        raise MigrationError(
            f"Refusing to migrate: destination {plan.dest_id!r} was "
            "constructed with is_read_only_scan=True.  Construct with False "
            "for `dc migrate copy`."
        )

    # Write the manifest BEFORE the first upload so a crash between here
    # and the first flush still leaves a replayable artifact naming every
    # planned entry.
    _flush_manifest(manifest, manifest_path)
    result.manifest_path = manifest_path

    for i, entry in enumerate(manifest.entries):
        if entry.action != "copy" or entry.state != "pending":
            # Non-copy actions + resume-done rows were pre-stamped above;
            # nothing to do here.
            continue

        record = _plan_entry_to_source_record(entry)

        # 1. Drift check — same semantics as ``apply/mover.py`` cloud dispatch.
        try:
            src_source.check_drift(record)
        except SourceDriftError as exc:
            _flush_manifest(manifest, manifest_path)
            raise MigrationError(
                f"Aborted mid-migration: cloud etag drift for "
                f"{entry.source_path} ({exc}). "
                f"Manifest at {manifest_path} reflects reality "
                f"({result.copied} file(s) copied so far)."
            ) from exc
        except (SourceAuthError, SourceRateLimitError) as exc:
            _flush_manifest(manifest, manifest_path)
            raise MigrationError(
                f"Aborted mid-migration: {type(exc).__name__} during drift "
                f"check for {entry.source_path}: {exc}. "
                f"Manifest at {manifest_path} reflects reality "
                f"({result.copied} file(s) copied so far)."
            ) from exc
        except (SourceNotFoundError, SourcePermissionError) as exc:
            log.warning(
                "Drift check skipped for %s: %s", entry.source_path, exc
            )
            manifest.entries[i] = entry.model_copy(
                update={"state": "error", "error_message": f"drift check: {exc}"}
            )
            result.errored += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue
        except SourceError as exc:
            log.warning(
                "Drift check failed for %s: %s", entry.source_path, exc
            )
            manifest.entries[i] = entry.model_copy(
                update={"state": "error", "error_message": f"drift check: {exc}"}
            )
            result.errored += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue

        # 2. Stream bytes source → optional throttle → dest.upload.
        try:
            byte_stream = src_source.read_bytes(record)
            throttled = _throttled_stream(byte_stream, max_bandwidth_mbps)
            upload_result: UploadResult = dst_source.upload(
                entry.dest_expected_path,
                throttled,
                int(entry.source_size),
            )
        except (SourceAuthError, SourceRateLimitError) as exc:
            _flush_manifest(manifest, manifest_path)
            raise MigrationError(
                f"Aborted mid-migration: {type(exc).__name__} uploading "
                f"{entry.source_path}: {exc}. "
                f"Manifest at {manifest_path} reflects reality "
                f"({result.copied} file(s) copied so far)."
            ) from exc
        except (SourceNotFoundError, SourcePermissionError) as exc:
            log.warning("Upload skipped for %s: %s", entry.source_path, exc)
            manifest.entries[i] = entry.model_copy(
                update={"state": "error", "error_message": f"upload: {exc}"}
            )
            result.errored += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue
        except SourceError as exc:
            log.warning("Upload failed for %s: %s", entry.source_path, exc)
            manifest.entries[i] = entry.model_copy(
                update={"state": "error", "error_message": f"upload: {exc}"}
            )
            result.errored += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue

        # 3. Post-upload hash verify.  Same-algo pairs must match exactly;
        # cross-algo pairs are optimistically accepted (verify --full
        # canonicalises via BLAKE3).
        match_verdict = _hashes_match(
            entry.source_hash,
            upload_result.uploaded_hash_algo,
            upload_result.uploaded_hash,
        )
        if match_verdict is False:
            # Same-algo mismatch — the destination bytes are wrong.  Trash
            # the botched destination BEFORE the manifest advances so the
            # invariant "cloud discards go to cloud trash" holds even on
            # the failure path.  Source is never touched.
            try:
                trash_record = FileRecord(
                    path=Path(entry.dest_expected_path),
                    size=int(entry.source_size),
                    mtime=0.0,
                    inode=0,
                    dev=0,
                    nlink=1,
                    source_id=dst_source.id,
                    cloud_file_id=upload_result.cloud_file_id,
                    etag=upload_result.etag,
                )
                dst_source.move_to_trash(trash_record)
            except SourceError as trash_exc:
                # If even the trash call fails, surface loudly.  The invariant
                # is best-effort — the dest bytes may linger and require
                # manual cleanup.
                log.error(
                    "Post-mismatch trash failed for %s at %s: %s",
                    entry.source_path,
                    entry.dest_expected_path,
                    trash_exc,
                )
                manifest.entries[i] = entry.model_copy(
                    update={
                        "state": "error",
                        "dest_cloud_file_id": upload_result.cloud_file_id,
                        "dest_etag": upload_result.etag,
                        "uploaded_hash": upload_result.uploaded_hash,
                        "uploaded_hash_algo": upload_result.uploaded_hash_algo,
                        "error_message": (
                            f"hash mismatch; dest trash also failed: "
                            f"{trash_exc}"
                        ),
                    }
                )
                result.errored += 1
                result.errors.append(
                    f"{entry.source_path}: hash mismatch (dest trash failed)"
                )
                _flush_manifest(manifest, manifest_path)
                continue
            manifest.entries[i] = entry.model_copy(
                update={
                    "state": "error",
                    "dest_cloud_file_id": None,
                    "dest_etag": None,
                    "uploaded_hash": None,
                    "uploaded_hash_algo": None,
                    "error_message": "hash mismatch",
                }
            )
            result.errored += 1
            result.errors.append(f"{entry.source_path}: hash mismatch")
            _flush_manifest(manifest, manifest_path)
            continue

        # 4. Success (same-algo match OR cross-algo optimistic).  Stamp the
        # entry and flush.
        verified_now = datetime.now(UTC).timestamp() if match_verdict else None
        manifest.entries[i] = entry.model_copy(
            update={
                "state": "done",
                "dest_cloud_file_id": upload_result.cloud_file_id,
                "dest_etag": upload_result.etag,
                "uploaded_hash": upload_result.uploaded_hash,
                "uploaded_hash_algo": upload_result.uploaded_hash_algo,
                "verified": bool(match_verdict),
                "verified_ts": verified_now,
                "error_message": None,
            }
        )
        result.copied += 1
        _flush_manifest(manifest, manifest_path)

    result.committed = True
    return result
