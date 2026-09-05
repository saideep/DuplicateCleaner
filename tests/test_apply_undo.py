from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.apply.undo import UndoError, restore_from_manifest
from duplicate_cleaner.paths import trash_dir_for
from duplicate_cleaner.report.schema import Report, ReportGroup, ReportMember


def _mkreport(tmp_path: Path) -> Path:
    a = tmp_path / "keep.txt"
    b = tmp_path / "discard.txt"
    a.write_bytes(b"data")
    b.write_bytes(b"data")
    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=b.stat().st_size,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=b.stat().st_size,
                reclaim_bytes=b.stat().st_size,
                members=[
                    ReportMember(
                        path=a,
                        size=a.stat().st_size,
                        mtime=a.stat().st_mtime,
                        hash="H" * 32,
                        score=1.0,
                        signals=[],
                        is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=b,
                        size=b.stat().st_size,
                        mtime=b.stat().st_mtime,
                        hash="H" * 32,
                        score=0.0,
                        signals=[],
                        is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


def test_apply_then_undo_restores_files(tmp_path: Path) -> None:
    report_path = _mkreport(tmp_path)
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    def trash_fn(p: Path) -> Path:
        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    runs = tmp_path / "runs"
    result = apply_report(
        report_path, commit=True, runs_dir=runs, trash_fn=trash_fn
    )

    assert result["committed"]
    assert result["moved"] == 1
    assert not (tmp_path / "discard.txt").exists()
    assert (fake_trash / "discard.txt").exists()
    assert (tmp_path / "keep.txt").exists()  # never touched

    manifest = Path(result["manifest_path"])
    assert manifest.exists()

    # H2: allow the tmp-dir Trash as a whitelisted Trash root.
    undo_result = restore_from_manifest(
        manifest, allowed_trash_dirs=[fake_trash]
    )
    assert undo_result["restored"] == 1
    assert undo_result["errors"] == []
    assert (tmp_path / "discard.txt").exists()
    assert (tmp_path / "discard.txt").read_bytes() == b"data"


def test_commit_refuses_when_paths_changed(tmp_path: Path) -> None:
    report_path = _mkreport(tmp_path)
    (tmp_path / "discard.txt").write_bytes(b"CHANGED_LONGER_CONTENT")
    with pytest.raises(ApplyError):
        apply_report(report_path, commit=True, runs_dir=tmp_path / "runs")


def test_undo_recovers_by_basename_when_trashed_at_path_missing(
    tmp_path: Path,
) -> None:
    """F17: crash mid-apply left manifest with trashed_at_path=None. Undo
    must locate the file in the volume's Trash by (basename, size, hash)."""
    import blake3  # type: ignore[import-untyped]

    original = tmp_path / "original.txt"
    original_content = b"important-data"

    # Simulate the file already being in the (fake) Trash.
    fake_trash = tmp_path / "fake_trash"
    fake_trash.mkdir()
    trashed_file = fake_trash / "original.txt"
    trashed_file.write_bytes(original_content)
    real_hash = blake3.blake3(original_content).hexdigest()

    # Manifest was written before the move but never updated post-move —
    # trashed_at_path is None. G4: the fallback uses the manifest hash to
    # confirm the candidate content matches — a real hash is required.
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": len(original_content),
                        "mtime": trashed_file.stat().st_mtime,
                        "hash": real_hash,
                        "trashed_at_path": None,
                    }
                ],
            }
        )
    )

    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 1
    assert result["errors"] == []
    assert original.exists()
    assert original.read_bytes() == original_content


def test_undo_refuses_to_restore_into_excluded_location(tmp_path: Path) -> None:
    """F12: even if a manifest names /System/foo as the original_path, undo
    must refuse. Zero side effects on the trashed file."""
    fake_trash = tmp_path / "fake_trash"
    fake_trash.mkdir()
    trashed = fake_trash / "foo.txt"
    trashed.write_bytes(b"data")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": "/System/Library/foo.txt",
                        "size": 4,
                        "mtime": 1000.0,
                        "hash": "H" * 32,
                        "trashed_at_path": str(trashed),
                    }
                ],
            }
        )
    )

    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 0
    assert len(result["errors"]) == 1
    assert "excluded" in result["errors"][0].lower()
    # Trashed file untouched — nothing was written into /System.
    assert trashed.exists()


def test_apply_refuses_report_with_excluded_or_out_of_root_path(
    tmp_path: Path,
) -> None:
    """F1: apply_report must reject a report that names a discard path
    outside the recorded scan roots. Zero side effects."""
    a = tmp_path / "inside.txt"
    a.write_bytes(b"data")
    outside = tmp_path.parent / "outside.txt"

    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=4,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=4,
                reclaim_bytes=4,
                members=[
                    ReportMember(
                        path=a,
                        size=4,
                        mtime=a.stat().st_mtime,
                        hash="H" * 32,
                        score=1.0,
                        signals=[],
                        is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=outside,
                        size=4,
                        mtime=1000.0,
                        hash="H" * 32,
                        score=0.0,
                        signals=[],
                        is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json())

    with pytest.raises(ApplyError):
        apply_report(report_path, commit=False, runs_dir=tmp_path / "runs")


def test_undo_refuses_trashed_at_path_outside_trash(tmp_path: Path) -> None:
    """H2: a poisoned manifest naming ``trashed_at_path`` outside every
    known Trash directory must be rejected. Without this check, a manifest
    pointing at ``~/.ssh/id_rsa`` would cause undo to ``shutil.move``
    that file into a scan root.
    """
    # Simulate: an attacker crafts a manifest whose ``trashed_at_path``
    # points at a sensitive file OUTSIDE any Trash directory.
    sensitive = tmp_path / "not_a_trash" / "id_rsa_lookalike"
    sensitive.parent.mkdir()
    sensitive.write_bytes(b"secret")
    fake_trash = tmp_path / "fake_trash"
    fake_trash.mkdir()

    original = tmp_path / "restored_target.txt"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": sensitive.stat().st_size,
                        "mtime": sensitive.stat().st_mtime,
                        "hash": "H" * 64,
                        "trashed_at_path": str(sensitive),
                    }
                ],
            }
        )
    )

    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    # Zero restores; the sensitive file is untouched; the original was
    # never created.
    assert result["restored"] == 0
    assert len(result["errors"]) == 1
    assert "Trash" in result["errors"][0] or "trash" in result["errors"][0].lower()
    assert sensitive.exists()
    assert sensitive.read_bytes() == b"secret"
    assert not original.exists()


def test_undo_refuses_manifest_relocating_via_symlink(tmp_path: Path) -> None:
    """H2: even if ``trashed_at_path`` names something inside a Trash-looking
    directory, if it's a symlink escaping to outside-Trash, resolve()
    catches it.
    """
    outside = tmp_path / "outside" / "victim.txt"
    outside.parent.mkdir()
    outside.write_bytes(b"outside-data")

    fake_trash = tmp_path / "fake_trash"
    fake_trash.mkdir()
    # A symlink INSIDE the trash pointing at a file OUTSIDE the trash.
    symlink_in_trash = fake_trash / "trojan_horse.txt"
    symlink_in_trash.symlink_to(outside)

    original = tmp_path / "restored_target.txt"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": outside.stat().st_size,
                        "mtime": outside.stat().st_mtime,
                        "hash": "H" * 64,
                        "trashed_at_path": str(symlink_in_trash),
                    }
                ],
            }
        )
    )

    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 0
    assert len(result["errors"]) == 1
    # resolve() sees through the symlink; the containment check fails.
    assert outside.exists()
    assert not original.exists()


def test_undo_refuses_archive_member_original_path(tmp_path: Path) -> None:
    """H5: manifest containing ``original_path`` with the archive-member
    separator ``::`` MUST be rejected up-front. Even one poisoned entry
    aborts the whole restore.
    """
    fake_trash = tmp_path / "fake_trash"
    fake_trash.mkdir()

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(tmp_path / "outer.zip") + "::etc/passwd",
                        "size": 4,
                        "mtime": 1000.0,
                        "hash": "H" * 64,
                        "trashed_at_path": None,
                    }
                ],
            }
        )
    )

    with pytest.raises(UndoError):
        restore_from_manifest(
            manifest,
            trash_dir_resolver=lambda _p: fake_trash,
            allowed_trash_dirs=[fake_trash],
        )


def test_trash_dir_for_boot_volume_vs_external() -> None:
    """F9: _trash_dir_for routes to ~/.Trash for boot-volume paths and to
    /Volumes/<VOL>/.Trashes/<uid>/ for external-drive paths."""
    import os as _os

    boot = trash_dir_for(Path("/tmp/foo"))
    assert boot == Path.home() / ".Trash"

    external = trash_dir_for(Path("/Volumes/MyDrive/some/file.txt"))
    uid = _os.getuid()
    assert external == Path("/Volumes/MyDrive/.Trashes") / str(uid)
