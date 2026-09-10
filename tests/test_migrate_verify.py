"""Tests for ``dc migrate verify`` — metadata + full-hash post-copy re-check."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import blake3  # type: ignore[import-untyped]
import pytest

from duplicate_cleaner.migrate.mover import _flush_manifest
from duplicate_cleaner.migrate.plan import (
    MigrationManifest,
    MigrationManifestEntry,
)
from duplicate_cleaner.migrate.verify import verify_migration
from duplicate_cleaner.sources.base import (
    SourceDriftError,
    SourceNotFoundError,
)


def _done_entry(
    *,
    dest_etag: str = "DST_A:t",
    source_hash: str | None = None,
) -> MigrationManifestEntry:
    return MigrationManifestEntry(
        source_id="gdrive:src",
        source_file_id="SRC_A",
        source_path="gdrive:src://a.pdf",
        source_etag="SRC_A:t",
        source_size=10,
        source_hash=source_hash,
        dest_expected_path="a.pdf",
        action="copy",
        state="done",
        dest_cloud_file_id="DST_A",
        dest_etag=dest_etag,
        uploaded_hash_algo="md5",
        uploaded_hash="a" * 32,
        verified=True,
        verified_ts=None,
    )


def _write_manifest(tmp_path: Path, entries: list[MigrationManifestEntry]) -> Path:
    manifest = MigrationManifest(
        plan_source_id="gdrive:src",
        plan_dest_id="gdrive:dst",
        entries=entries,
    )
    p = tmp_path / "manifest.json"
    _flush_manifest(manifest, p)
    return p


class _FakeDst:
    def __init__(self) -> None:
        self.id = "gdrive:dst"
        self.is_read_only_scan = False
        self.check_drift = MagicMock(return_value=None)
        self._bytes: bytes = b""

    def read_bytes(
        self, record: object, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        yield self._bytes


def test_verify_matches_hash_marks_verified_true(tmp_path: Path) -> None:
    """Metadata-only mode: no drift → verified=True + verified_ts set."""
    manifest_path = _write_manifest(tmp_path, [_done_entry()])
    dst = _FakeDst()
    result = verify_migration(
        manifest_path,
        sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
    )
    assert result.verified == 1
    assert result.drifted == 0
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is True
    assert e.verified_ts is not None


def test_verify_etag_drift_marks_false(tmp_path: Path) -> None:
    """SourceDriftError from destination check_drift → verified=False."""
    manifest_path = _write_manifest(tmp_path, [_done_entry()])
    dst = _FakeDst()
    dst.check_drift.side_effect = SourceDriftError("etag now DST_A:t2")
    result = verify_migration(
        manifest_path,
        sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
    )
    assert result.drifted == 1
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is False
    assert e.error_message and "drift" in e.error_message.lower()


def test_verify_missing_dest_marks_false(tmp_path: Path) -> None:
    """SourceNotFoundError → verified=False + error 'dest file no longer exists'."""
    manifest_path = _write_manifest(tmp_path, [_done_entry()])
    dst = _FakeDst()
    dst.check_drift.side_effect = SourceNotFoundError("deleted")
    result = verify_migration(
        manifest_path,
        sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
    )
    assert result.missing == 1
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is False
    assert e.error_message and "no longer exists" in e.error_message.lower()


def test_verify_full_mode_streams_dest(tmp_path: Path) -> None:
    """--full path: dst.read_bytes is called and BLAKE3 is checked."""
    payload = b"hello world"
    src_hex = blake3.blake3(payload).hexdigest()
    entry = _done_entry(source_hash=src_hex)  # bare blake3 hex → local semantics
    manifest_path = _write_manifest(tmp_path, [entry])
    dst = _FakeDst()
    dst._bytes = payload
    result = verify_migration(
        manifest_path,
        sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
        full=True,
    )
    assert result.verified == 1
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is True
    # ``dest_blake3`` persisted as an audit trail even on the happy path.
    assert e.dest_blake3 == src_hex


def test_verify_propagates_unexpected_exceptions(tmp_path: Path) -> None:
    """Audit pass 15 finding #4: KeyError from check_drift propagates.

    A real bug in the source implementation (e.g. mis-shaped Graph response
    triggering a KeyError, or a bad etag parse raising ValueError) must NOT
    be silently converted into ``verified=False`` + ``"dest etag drifted"``.
    The exception propagates and aborts the verify pass so the bug surfaces.
    """
    manifest_path = _write_manifest(tmp_path, [_done_entry()])
    dst = _FakeDst()
    dst.check_drift.side_effect = KeyError("mis-shaped response")
    with pytest.raises(KeyError):
        verify_migration(
            manifest_path,
            sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
        )
    # Manifest was NOT flipped to verified=False — the bug bubbles.
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is True  # untouched from the seed
    assert e.error_message is None


def test_verify_full_cross_algo_matches_hashes(tmp_path: Path) -> None:
    """Audit pass 15 finding #3 / L2: cross-algo pair verifies via BLAKE3.

    Source hash is ``md5:``; source_blake3 stamped at copy time equals the
    destination bytes' BLAKE3 — ``dc migrate verify --full`` confirms
    ``verified=True`` on the strength of the real byte-level compare.
    """
    payload = b"payload bytes for cross-algo verify"
    canonical = blake3.blake3(payload).hexdigest()
    entry = _done_entry(source_hash="md5:" + "a" * 32).model_copy(
        update={"source_blake3": canonical, "verified": False}
    )
    manifest_path = _write_manifest(tmp_path, [entry])
    dst = _FakeDst()
    dst._bytes = payload
    result = verify_migration(
        manifest_path,
        sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
        full=True,
    )
    assert result.verified == 1
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is True
    assert e.dest_blake3 == canonical
    assert e.source_blake3 == canonical


def test_verify_full_cross_algo_mismatch_marks_false(tmp_path: Path) -> None:
    """Audit pass 15 finding #3 / L2: cross-algo BLAKE3 mismatch → verified=False.

    source_blake3 stamped at copy time differs from what the destination
    bytes now hash to — a real bit-flip.  ``verify --full`` demotes the
    entry to ``verified=False`` and stamps an error message naming both
    truncated hex digests.
    """
    dest_payload = b"corrupted destination bytes"
    wrong_dest_hex = blake3.blake3(dest_payload).hexdigest()
    stamped_source_hex = blake3.blake3(b"original source bytes").hexdigest()
    entry = _done_entry(source_hash="md5:" + "a" * 32).model_copy(
        update={"source_blake3": stamped_source_hex, "verified": False}
    )
    manifest_path = _write_manifest(tmp_path, [entry])
    dst = _FakeDst()
    dst._bytes = dest_payload
    result = verify_migration(
        manifest_path,
        sources_by_id={"gdrive:src": MagicMock(), "gdrive:dst": dst},
        full=True,
    )
    assert result.errored == 1
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.verified is False
    assert e.dest_blake3 == wrong_dest_hex
    assert e.error_message and "BLAKE3 mismatch" in e.error_message
