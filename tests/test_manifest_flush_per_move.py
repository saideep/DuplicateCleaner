"""G6: manifest is fsync-flushed after every successful trash move.

The prior design flushed the manifest once before the loop and once after,
so a kernel panic mid-loop left every entry with ``trashed_at_path=None``
on disk. Recovery would then rely entirely on the (imperfect) basename
fallback. G6 flushes per-move so the manifest on disk always reflects
reality up to the last crash boundary.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from duplicate_cleaner.apply.mover import apply_report
from duplicate_cleaner.report.schema import Report, ReportGroup, ReportMember


def _mkreport(tmp_path: Path, n: int = 3) -> Path:
    """Build a report with one keeper and ``n`` discard members."""
    keeper = tmp_path / "keep.txt"
    keeper.write_bytes(b"data")
    members = [
        ReportMember(
            path=keeper,
            size=keeper.stat().st_size,
            mtime=keeper.stat().st_mtime,
            hash="H" * 32,
            score=1.0,
            signals=[],
            is_proposed_keeper=True,
        )
    ]
    for i in range(n):
        d = tmp_path / f"discard_{i}.txt"
        d.write_bytes(b"data")
        members.append(
            ReportMember(
                path=d,
                size=d.stat().st_size,
                mtime=d.stat().st_mtime,
                hash="H" * 32,
                score=0.0,
                signals=[],
                is_proposed_keeper=False,
            )
        )
    report = Report(
        roots=[tmp_path],
        total_files_scanned=n + 1,
        total_groups=1,
        total_reclaim_bytes=4 * n,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=4,
                reclaim_bytes=4 * n,
                members=members,
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


def test_manifest_is_flushed_after_every_successful_move(tmp_path: Path) -> None:
    """Count fsync calls; per-move flushing is guaranteed."""
    report_path = _mkreport(tmp_path, n=3)
    runs = tmp_path / "runs"

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    def trash_fn(p: Path) -> Path:
        import shutil

        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    fsync_calls: list[int] = []
    real_fsync = __import__("os").fsync

    def counting_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    with patch("duplicate_cleaner.apply.mover.os.fsync", side_effect=counting_fsync):
        result = apply_report(
            report_path, commit=True, runs_dir=runs, trash_fn=trash_fn
        )
    assert result["moved"] == 3

    # 3 moves → at least 3 in-loop flushes (each _write_manifest issues 2
    # fsyncs: file + directory). Plus the pre-loop and post-loop flushes.
    # We assert >= 3*2 = 6 to prove per-move flushing is happening, not
    # just the endpoints.
    assert len(fsync_calls) >= 6, (
        f"expected per-move fsync flushes; got {len(fsync_calls)} fsync calls"
    )
