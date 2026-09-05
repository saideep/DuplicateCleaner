from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.apply.mover import apply_report
from duplicate_cleaner.report.schema import (
    Report,
    ReportGroup,
    ReportMember,
)


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


def test_dry_run_moves_nothing_and_writes_no_manifest(tmp_path: Path) -> None:
    report_path = _mkreport(tmp_path)
    runs = tmp_path / "runs"
    result = apply_report(report_path, commit=False, runs_dir=runs)

    assert result["committed"] is False
    assert result["manifest_path"] is None
    assert result["moved"] == 0
    assert result["planned"] == 1
    assert result["verified"] == 1
    # Both files still exist.
    assert (tmp_path / "keep.txt").exists()
    assert (tmp_path / "discard.txt").exists()
    # runs directory was not created for a dry-run.
    assert not runs.exists()


def test_dry_run_flags_changed_paths(tmp_path: Path) -> None:
    report_path = _mkreport(tmp_path)
    # Modify the discard file after "scan".
    (tmp_path / "discard.txt").write_bytes(b"CHANGED_CONTENT_DIFFERENT_SIZE")

    result = apply_report(report_path, commit=False, runs_dir=tmp_path / "runs")
    assert result["verified"] == 0
    assert len(result["changed_or_missing"]) == 1
