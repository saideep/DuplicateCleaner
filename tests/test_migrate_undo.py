"""Tests for ``dc migrate undo`` — reverse copy + cleanup."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from duplicate_cleaner.migrate.mover import _flush_manifest
from duplicate_cleaner.migrate.plan import (
    MigrationManifest,
    MigrationManifestEntry,
)
from duplicate_cleaner.migrate.undo import undo_migration
from duplicate_cleaner.sources.base import TrashedLocation


def _entry(
    name: str,
    *,
    state: str = "done",
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
        state=state,  # type: ignore[arg-type]
        dest_cloud_file_id=(f"DST_{name}" if state == "done" else None),
        dest_etag=(f"DST_{name}:t" if state == "done" else None),
        verified=(state == "done"),
        verified_ts=1234.0 if state == "done" else None,
        cleanup_done=cleanup_done,
        source_cloud_trash_id=(f"SRC_{name}" if cleanup_done else None),
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


class _FakeSource:
    def __init__(self, source_id: str) -> None:
        self.id = source_id
        self.is_read_only_scan = False
        self.move_to_trash = MagicMock(
            return_value=TrashedLocation(
                source_id=source_id,
                original_path="",
                cloud_file_id="TRASHED",
                cloud_trash_id="TRASHED",
            )
        )
        self.restore_from_trash = MagicMock(return_value=None)


def test_undo_reverses_copy_by_trashing_dest(tmp_path: Path) -> None:
    """A done entry (no cleanup) → destination trashed on undo."""
    manifest_path = _write_manifest(tmp_path, [_entry("a.pdf")])
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    result = undo_migration(
        manifest_path,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.trashed_dest == 1
    assert result.restored_source == 0
    dst.move_to_trash.assert_called_once()
    src.restore_from_trash.assert_not_called()


def test_undo_restores_source_when_cleanup_happened(tmp_path: Path) -> None:
    """cleanup_done=True → source.restore_from_trash called AND dest trashed."""
    manifest_path = _write_manifest(
        tmp_path, [_entry("a.pdf", cleanup_done=True)]
    )
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    result = undo_migration(
        manifest_path,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.restored_source == 1
    assert result.trashed_dest == 1
    src.restore_from_trash.assert_called_once()
    loc_arg = src.restore_from_trash.call_args.args[0]
    assert loc_arg.source_id == "gdrive:src"
    assert loc_arg.cloud_trash_id == "SRC_a.pdf"
    dst.move_to_trash.assert_called_once()


def test_undo_partial_state(tmp_path: Path) -> None:
    """Mixed manifest: one cleanup_done, one just done."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            _entry("a.pdf", cleanup_done=True),
            _entry("b.pdf", cleanup_done=False),
        ],
    )
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    result = undo_migration(
        manifest_path,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.restored_source == 1
    assert result.trashed_dest == 2


def test_undo_ignores_error_entries(tmp_path: Path) -> None:
    """Entries with state='error' or 'pending' or 'skipped' are untouched."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            _entry("err.pdf", state="error"),
            _entry("skip.pdf", state="skipped"),
            _entry("pending.pdf", state="pending"),
        ],
    )
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    result = undo_migration(
        manifest_path,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.trashed_dest == 0
    assert result.restored_source == 0
    dst.move_to_trash.assert_not_called()
    src.restore_from_trash.assert_not_called()
