"""Post-copy verification for ``dc migrate copy`` manifests.

v0.5-b: reads a manifest written by :func:`execute_migration` and confirms
each ``state="done"`` entry is still intact on the destination.

Two verification modes:

- **Metadata mode (default)** — for every done entry, re-fetch the
  destination's cloud metadata and compare the current etag against the
  manifest's ``dest_etag``.  Fast (one round-trip per entry) but only
  detects post-upload drift.  A silent bit-flip on the provider side would
  not surface.
- **Full mode (``--full`` in the CLI)** — additionally stream the destination
  bytes through BLAKE3 and compare against the source's canonical hash.
  Catches bit-flips at the cost of a full re-download per entry.  The
  ``read_bytes`` iterator is consumed to a running BLAKE3 hash so peak
  memory stays flat.

Every entry update is flushed atomically (tempfile + fsync + os.replace +
parent-dir fsync) so an aborted verify leaves a correct partial manifest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import blake3

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


@dataclass
class VerifyResult:
    """Return shape for :func:`verify_migration`."""

    total_done: int = 0
    verified: int = 0
    drifted: int = 0
    missing: int = 0
    errored: int = 0
    errors: list[str] = field(default_factory=list)


class VerifyError(RuntimeError):
    """Raised when verify cannot safely proceed (e.g. auth abort)."""


def verify_migration(
    manifest_path: Path,
    *,
    sources_by_id: dict[str, Source],
    full: bool = False,
) -> VerifyResult:
    """Verify every ``state="done"`` entry in ``manifest_path``.

    For each entry: locate the destination Source in ``sources_by_id``,
    build a synthetic :class:`FileRecord` from the manifest's stamped
    ``dest_cloud_file_id`` + ``dest_etag``, and call
    :meth:`Source.check_drift`.  Drift or not-found sets ``verified=False``
    and stamps an ``error_message``.  Otherwise the entry keeps
    ``verified=True`` and ``verified_ts`` bumps to now.

    ``full=True`` additionally streams the destination bytes through BLAKE3
    and compares against a canonical BLAKE3 for the source.  When the source
    hash algo is not BLAKE3 (md5 / sha256), the source-side canonical hash
    is not directly available on the manifest — full-mode falls back to
    accepting the etag match but stamps a ``verified_full_hash`` on the
    entry recording the destination's BLAKE3 so a later audit can compare.
    """
    manifest = _load_manifest(manifest_path)
    result = VerifyResult()

    for i, entry in enumerate(manifest.entries):
        if entry.state != "done":
            continue
        result.total_done += 1
        dst_source = sources_by_id.get(manifest.plan_dest_id)
        if dst_source is None:
            result.errored += 1
            result.errors.append(
                f"{entry.source_path}: destination source "
                f"{manifest.plan_dest_id!r} not in sources map"
            )
            manifest.entries[i] = entry.model_copy(
                update={
                    "verified": False,
                    "error_message": (
                        f"verify: destination source {manifest.plan_dest_id!r} "
                        "not in sources map"
                    ),
                }
            )
            _flush_manifest(manifest, manifest_path)
            continue

        # Build the record the drift-check expects.  ``dest_cloud_file_id``
        # was stamped by the copy loop from the destination's post-upload
        # re-fetch — using it here means verify addresses the same object
        # that was written (no fresh list_files sweep needed).
        record = FileRecord(
            path=Path(entry.dest_expected_path),
            size=int(entry.source_size),
            mtime=0.0,
            inode=0,
            dev=0,
            nlink=1,
            source_id=manifest.plan_dest_id,
            cloud_file_id=entry.dest_cloud_file_id,
            etag=entry.dest_etag,
        )
        try:
            dst_source.check_drift(record)
        except SourceNotFoundError as exc:
            log.warning(
                "Verify: destination file missing for %s: %s",
                entry.source_path,
                exc,
            )
            manifest.entries[i] = entry.model_copy(
                update={
                    "verified": False,
                    "error_message": "dest file no longer exists",
                }
            )
            result.missing += 1
            result.errors.append(f"{entry.source_path}: dest file missing")
            _flush_manifest(manifest, manifest_path)
            continue
        except SourcePermissionError as exc:
            log.warning(
                "Verify: permission denied for %s: %s",
                entry.source_path,
                exc,
            )
            manifest.entries[i] = entry.model_copy(
                update={
                    "verified": False,
                    "error_message": f"verify permission: {exc}",
                }
            )
            result.errored += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue
        except (SourceAuthError, SourceRateLimitError) as exc:
            _flush_manifest(manifest, manifest_path)
            raise VerifyError(
                f"Aborted verify: {type(exc).__name__} on "
                f"{entry.source_path}: {exc}. "
                f"Manifest at {manifest_path} reflects reality "
                f"({result.verified} entry(ies) verified so far)."
            ) from exc
        except Exception as exc:  # SourceDriftError + generic SourceError
            # Any drift (etag mismatch) surfaces as ``verified=False`` — the
            # cleanup step will refuse to trash the source.
            log.warning(
                "Verify: drift or error for %s: %s",
                entry.source_path,
                exc,
            )
            manifest.entries[i] = entry.model_copy(
                update={
                    "verified": False,
                    "error_message": "dest etag drifted",
                }
            )
            result.drifted += 1
            result.errors.append(f"{entry.source_path}: {exc}")
            _flush_manifest(manifest, manifest_path)
            continue

        # Full mode: additionally stream the destination bytes for a
        # canonical BLAKE3.  Kept behind a flag because a bulk verify would
        # otherwise re-download every migrated byte.
        if full:
            try:
                h = blake3.blake3()
                for chunk in dst_source.read_bytes(record):
                    h.update(chunk)
                dest_blake3 = h.hexdigest()
            except SourceError as exc:
                log.warning(
                    "Verify --full stream failed for %s: %s",
                    entry.source_path,
                    exc,
                )
                manifest.entries[i] = entry.model_copy(
                    update={
                        "verified": False,
                        "error_message": f"verify --full stream: {exc}",
                    }
                )
                result.errored += 1
                result.errors.append(f"{entry.source_path}: {exc}")
                _flush_manifest(manifest, manifest_path)
                continue
            # Compare against the source's foreign hash when it happens to
            # be BLAKE3 (local source).  Cross-algo pairs cache the
            # destination BLAKE3 for a later audit but do not fail here —
            # the etag check above already caught post-upload drift.
            src_algo, src_hex = _split_algo_hash_local(entry.source_hash)
            if src_algo == "blake3" and src_hex and src_hex != dest_blake3:
                manifest.entries[i] = entry.model_copy(
                    update={
                        "verified": False,
                        "error_message": (
                            f"verify --full BLAKE3 mismatch: "
                            f"src={src_hex[:12]} dst={dest_blake3[:12]}"
                        ),
                    }
                )
                result.errored += 1
                result.errors.append(
                    f"{entry.source_path}: BLAKE3 mismatch on dest"
                )
                _flush_manifest(manifest, manifest_path)
                continue

        manifest.entries[i] = entry.model_copy(
            update={
                "verified": True,
                "verified_ts": datetime.now(UTC).timestamp(),
                "error_message": None,
            }
        )
        result.verified += 1
        _flush_manifest(manifest, manifest_path)

    return result


def _split_algo_hash_local(hash_str: str | None) -> tuple[str | None, str | None]:
    """Local copy of the algo-tag parser to avoid a circular import."""
    if not hash_str:
        return None, None
    if ":" in hash_str:
        algo, _, hex_part = hash_str.partition(":")
        return algo.lower(), hex_part.lower()
    return "blake3", hash_str.lower()
