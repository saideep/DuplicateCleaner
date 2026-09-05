"""Poisoned report.json protection.

If a user (or attacker) edits the JSON report to redirect a discard path at
``~/Library/...`` or another hard-coded excluded location, ``apply --commit``
must refuse with ``ApplyError`` and touch NOTHING on disk.

Zero side effects — no run directory, no manifest, no trash calls.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.report.schema import Report, ReportGroup, ReportMember


def _forbid_all_trash(_p: Path) -> Path:
    raise AssertionError("trash_fn must not be called for a poisoned report")


def _build_poisoned_report(
    tmp_path: Path, poisoned_discard: Path
) -> Path:
    """Build a report where the discard path targets an excluded location."""
    a = tmp_path / "keep.txt"
    a.write_bytes(b"data")

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
                    # Poisoned — a hand-edited JSON pointed the discard at
                    # ~/Library or /System. apply_report must reject.
                    ReportMember(
                        path=poisoned_discard,
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
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


@pytest.mark.parametrize(
    "poisoned_relative",
    [
        # ~/Library subtree — the most likely accident.
        Path.home() / "Library" / "Application Support" / "Firefox" / "foo.txt",
        # Cross-user library (another account on the box).
        Path("/Users/other/Library/foo.txt"),
        # System roots.
        Path("/System/foo.txt"),
        Path("/private/etc/passwd"),
        Path("/etc/hosts"),
    ],
)
def test_apply_refuses_poisoned_excluded_discard(
    tmp_path: Path, poisoned_relative: Path
) -> None:
    """Any excluded target in the report must abort apply with zero writes."""
    runs_dir = tmp_path / "runs"
    report_path = _build_poisoned_report(tmp_path, poisoned_relative)

    with pytest.raises(ApplyError):
        apply_report(
            report_path,
            commit=True,
            runs_dir=runs_dir,
            trash_fn=_forbid_all_trash,
        )

    # No run dir, no manifest — apply aborted before any side effect.
    assert not runs_dir.exists()
    # Keep file untouched.
    assert (tmp_path / "keep.txt").exists()


def test_apply_refuses_discard_path_outside_recorded_roots(
    tmp_path: Path,
) -> None:
    """Discard path is a real, non-excluded location but sits OUTSIDE the
    recorded scan roots. That is also a tampering signal — must reject.
    """
    outside = tmp_path.parent / "sibling.txt"
    # Do not create it; we still expect the validator to reject on containment
    # before it even stats the file.
    report_path = _build_poisoned_report(tmp_path, outside)
    with pytest.raises(ApplyError):
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            trash_fn=_forbid_all_trash,
        )
    assert not (tmp_path / "runs").exists()


def _build_report_with_roots(
    tmp_path: Path, roots: list[str]
) -> Path:
    """Report with hand-picked ``roots`` list — used to test G1 rejection."""
    a = tmp_path / "keep.txt"
    b = tmp_path / "discard.txt"
    a.write_bytes(b"data")
    b.write_bytes(b"data")
    report = Report(
        roots=[Path(r) for r in roots],
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
                        path=b,
                        size=4,
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


@pytest.mark.parametrize(
    "root",
    [
        "/",           # G1: root filesystem — every discard would "belong" here
        "/Users",      # 2 segments, too shallow
        "/Volumes",    # 2 segments, too shallow
        "/System",     # excluded system root
        str(Path.home() / "Library"),  # excluded user Library
        "/private/var/folders",        # G2: newly re-blocked
    ],
)
def test_apply_refuses_poisoned_report_roots(tmp_path: Path, root: str) -> None:
    """G1: report.roots itself must be validated before any per-member loop.

    A poisoned or mistyped JSON with ``roots=["/"]`` would otherwise pass
    every ``is_within`` check and permit trashing any file on disk.
    """
    report_path = _build_report_with_roots(tmp_path, [root])
    runs = tmp_path / "runs"
    with pytest.raises(ApplyError):
        apply_report(
            report_path,
            commit=True,
            runs_dir=runs,
            trash_fn=_forbid_all_trash,
        )
    # Bad roots aborts before ANY side effect.
    assert not runs.exists()
    assert (tmp_path / "keep.txt").exists()
    assert (tmp_path / "discard.txt").exists()
