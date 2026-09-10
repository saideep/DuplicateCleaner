"""Tests for ``dc migrate cleanup`` — trash source originals after verify."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.migrate.cleanup import (
    CleanupError,
    cleanup_source_after_migration,
)
from duplicate_cleaner.migrate.mover import _flush_manifest
from duplicate_cleaner.migrate.plan import (
    MigrationManifest,
    MigrationManifestEntry,
)
from duplicate_cleaner.sources.base import TrashedLocation


def _done_entry(
    name: str = "a.pdf",
    *,
    verified: bool = True,
    verified_ts: float | None = 1234567.0,
    cleanup_done: bool = False,
) -> MigrationManifestEntry:
    return MigrationManifestEntry(
        source_id="gdrive:src",
        source_file_id=f"SRC_{name}",
        source_path=f"gdrive:src://{name}",
        source_etag=f"SRC_{name}:t",
        source_size=10,
        source_hash="md5:" + "a" * 32,
        dest_expected_path=name,
        action="copy",
        state="done",
        dest_cloud_file_id=f"DST_{name}",
        dest_etag=f"DST_{name}:t",
        uploaded_hash_algo="md5",
        uploaded_hash="a" * 32,
        verified=verified,
        verified_ts=verified_ts,
        cleanup_done=cleanup_done,
    )


def _write_manifest(
    tmp_path: Path, entries: list[MigrationManifestEntry]
) -> Path:
    manifest = MigrationManifest(
        plan_source_id="gdrive:src",
        plan_dest_id="gdrive:dst",
        entries=entries,
    )
    p = tmp_path / "manifest.json"
    _flush_manifest(manifest, p)
    return p


class _FakeSrc:
    def __init__(self) -> None:
        self.id = "gdrive:src"
        self.is_read_only_scan = False
        self.move_to_trash = MagicMock(
            return_value=TrashedLocation(
                source_id="gdrive:src",
                original_path="",
                cloud_file_id="TRASHED_ID",
                cloud_trash_id="TRASHED_ID",
            )
        )


def test_cleanup_refuses_without_verify(tmp_path: Path) -> None:
    """A done entry with verified=False raises CleanupError."""
    manifest_path = _write_manifest(
        tmp_path,
        [_done_entry(verified=False)],
    )
    src = _FakeSrc()
    with pytest.raises(CleanupError):
        cleanup_source_after_migration(
            manifest_path,
            commit=False,
            sources_by_id={"gdrive:src": src},
        )
    src.move_to_trash.assert_not_called()


def test_cleanup_refuses_without_verify_ts(tmp_path: Path) -> None:
    """A done entry with verified=True but verified_ts=None also raises."""
    manifest_path = _write_manifest(
        tmp_path,
        [_done_entry(verified_ts=None)],
    )
    src = _FakeSrc()
    with pytest.raises(CleanupError):
        cleanup_source_after_migration(
            manifest_path,
            commit=False,
            sources_by_id={"gdrive:src": src},
        )


def test_cleanup_dry_run_no_trash(tmp_path: Path) -> None:
    """Dry-run: verify passes but no move_to_trash call fires."""
    manifest_path = _write_manifest(tmp_path, [_done_entry()])
    src = _FakeSrc()
    result = cleanup_source_after_migration(
        manifest_path,
        commit=False,
        sources_by_id={"gdrive:src": src},
    )
    assert result.planned == 1
    assert result.trashed == 0
    assert result.committed is False
    src.move_to_trash.assert_not_called()


def test_cleanup_commit_trashes_sources(tmp_path: Path) -> None:
    """Commit: source.move_to_trash called per done+verified entry."""
    manifest_path = _write_manifest(
        tmp_path,
        [_done_entry("a.pdf"), _done_entry("b.pdf")],
    )
    src = _FakeSrc()
    result = cleanup_source_after_migration(
        manifest_path,
        commit=True,
        sources_by_id={"gdrive:src": src},
    )
    assert result.trashed == 2
    assert src.move_to_trash.call_count == 2
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    for e in manifest.entries:
        assert e.cleanup_done is True
        assert e.source_cloud_trash_id == "TRASHED_ID"


def test_cleanup_refuses_when_verified_false_after_cross_algo_copy(
    tmp_path: Path,
) -> None:
    """Audit pass 15 finding #7: cross-algo copy → cleanup refuses without verify.

    ``dc migrate copy`` leaves cross-algo entries ``verified=False`` even
    on a successful upload — the byte-level check is deferred to
    ``dc migrate verify --full``.  ``cleanup`` MUST refuse to trash the
    source in this state so a hand-crafted "skip verify" workflow cannot
    silently trash the source original against an unverified copy.
    """
    # Simulate the state a cross-algo copy leaves behind: state='done',
    # verified=False, verified_ts=None, source_blake3 stamped for future
    # verify --full.
    entry = _done_entry("cross.pdf", verified=False, verified_ts=None).model_copy(
        update={"source_blake3": "b" * 64},
    )
    manifest_path = _write_manifest(tmp_path, [entry])
    src = _FakeSrc()
    with pytest.raises(CleanupError, match="verify"):
        cleanup_source_after_migration(
            manifest_path,
            commit=True,
            sources_by_id={"gdrive:src": src},
        )
    src.move_to_trash.assert_not_called()
