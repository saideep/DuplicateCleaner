"""Apply + undo integration for image near-dup groups (v0.7)."""
from __future__ import annotations

import shutil
from pathlib import Path

from duplicate_cleaner.apply.mover import apply_report
from duplicate_cleaner.apply.undo import restore_from_manifest
from duplicate_cleaner.report.schema import (
    ImageNearDupSignal,
    Report,
    ReportGroup,
    ReportMember,
    ReportSignal,
)


def _mk_near_dup_report(
    tmp_path: Path,
    keeper: Path,
    discard: Path,
) -> Path:
    """Emit a report.json containing a single ``kind="image-near-dup"`` group."""
    keeper.parent.mkdir(parents=True, exist_ok=True)
    discard.parent.mkdir(parents=True, exist_ok=True)
    keeper.write_bytes(b"keeper-image-bytes" * 500)
    discard.write_bytes(b"reencoded-image-bytes" * 400)

    keeper_stat = keeper.stat()
    discard_stat = discard.stat()

    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=discard_stat.st_size,
        groups=[],
        image_near_dup_groups=[
            ReportGroup(
                id="image-near-dup-0000",
                kind="image-near-dup",
                size=keeper_stat.st_size,
                hash="image-near-dup-0000",
                reclaim_bytes=discard_stat.st_size,
                similarity_pct=97.0,
                image_near_dup=ImageNearDupSignal(
                    max_pairwise_distance=6,
                    hash_bits=256,
                    min_size=discard_stat.st_size,
                    max_size=keeper_stat.st_size,
                ),
                members=[
                    ReportMember(
                        path=keeper,
                        size=keeper_stat.st_size,
                        mtime=keeper_stat.st_mtime,
                        hash="KEEPER_HASH",
                        score=0.0,
                        signals=[ReportSignal(name="keeper", contribution=0.0)],
                        is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=discard,
                        size=discard_stat.st_size,
                        mtime=discard_stat.st_mtime,
                        hash="DISCARD_HASH",
                        score=-1.5,
                        signals=[
                            ReportSignal(
                                name="perceptual near-dup (distance 6/256)",
                                contribution=-1.5,
                            )
                        ],
                        is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


def test_apply_image_near_dup_group_moves_discards_to_trash(tmp_path: Path) -> None:
    """A near-dup discard flows through the same per-file trash_fn as an exact discard."""
    keeper = tmp_path / "live" / "orig.png"
    discard = tmp_path / "live" / "reencoded.jpg"
    report_path = _mk_near_dup_report(tmp_path, keeper, discard)

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    trashed: list[Path] = []

    def trash_fn(p: Path) -> Path:
        trashed.append(p)
        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    runs = tmp_path / "runs"
    result = apply_report(
        report_path,
        commit=True,
        runs_dir=runs,
        trash_fn=trash_fn,
    )
    assert result["committed"]
    assert result["moved"] == 1
    # The keeper survives; the discard is in the fake trash.
    assert keeper.exists()
    assert not discard.exists()
    assert (fake_trash / discard.name).exists()
    # trash_fn saw exactly one file — the discard.
    assert trashed == [discard]


def test_apply_image_near_dup_dry_run_moves_nothing(tmp_path: Path) -> None:
    keeper = tmp_path / "live" / "orig.png"
    discard = tmp_path / "live" / "reencoded.jpg"
    report_path = _mk_near_dup_report(tmp_path, keeper, discard)

    result = apply_report(report_path, commit=False, runs_dir=tmp_path / "runs")
    assert result["committed"] is False
    assert result["planned"] == 1
    assert keeper.exists()
    assert discard.exists()


def test_undo_image_near_dup_restores(tmp_path: Path) -> None:
    """Apply + undo round-trip restores the discarded near-dup to its original path."""
    keeper = tmp_path / "live" / "orig.png"
    discard = tmp_path / "live" / "reencoded.jpg"
    report_path = _mk_near_dup_report(tmp_path, keeper, discard)

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    def trash_fn(p: Path) -> Path:
        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    runs = tmp_path / "runs"
    result = apply_report(
        report_path,
        commit=True,
        runs_dir=runs,
        trash_fn=trash_fn,
    )
    manifest_path = Path(result["manifest_path"])
    assert not discard.exists()
    assert (fake_trash / discard.name).exists()

    # Undo — pass allowed_trash_dirs so the test's fake trash is honoured.
    undo_result = restore_from_manifest(
        manifest_path,
        allowed_trash_dirs=[fake_trash],
    )
    assert undo_result["restored"] == 1
    assert discard.exists()
    assert not (fake_trash / discard.name).exists()


def test_apply_image_near_dup_alongside_exact_group(tmp_path: Path) -> None:
    """Image near-dup group + a plain exact group both move to trash in one apply."""
    # Exact group.
    keeper_a = tmp_path / "live" / "exact_keep.txt"
    discard_a = tmp_path / "live" / "exact_discard.txt"
    keeper_a.parent.mkdir(parents=True, exist_ok=True)
    keeper_a.write_bytes(b"same data")
    discard_a.write_bytes(b"same data")

    # Image near-dup group.
    keeper_b = tmp_path / "live" / "orig.png"
    discard_b = tmp_path / "live" / "reencoded.jpg"
    keeper_b.write_bytes(b"keeper-img" * 200)
    discard_b.write_bytes(b"discard-img" * 150)

    ka, da = keeper_a.stat(), discard_a.stat()
    kb, db = keeper_b.stat(), discard_b.stat()

    report = Report(
        roots=[tmp_path],
        total_files_scanned=4,
        total_groups=2,
        total_reclaim_bytes=da.st_size + db.st_size,
        groups=[
            ReportGroup(
                id="g1",
                kind="exact",
                hash="EX",
                size=da.st_size,
                reclaim_bytes=da.st_size,
                members=[
                    ReportMember(
                        path=keeper_a, size=ka.st_size, mtime=ka.st_mtime,
                        hash="EX", score=1.0, signals=[], is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=discard_a, size=da.st_size, mtime=da.st_mtime,
                        hash="EX", score=0.0, signals=[], is_proposed_keeper=False,
                    ),
                ],
            )
        ],
        image_near_dup_groups=[
            ReportGroup(
                id="image-near-dup-0000",
                kind="image-near-dup",
                hash="image-near-dup-0000",
                size=kb.st_size,
                reclaim_bytes=db.st_size,
                similarity_pct=95.0,
                members=[
                    ReportMember(
                        path=keeper_b, size=kb.st_size, mtime=kb.st_mtime,
                        hash="KB", score=0.0, signals=[], is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=discard_b, size=db.st_size, mtime=db.st_mtime,
                        hash="DB", score=-1.0, signals=[], is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json())

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    def trash_fn(p: Path) -> Path:
        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    result = apply_report(
        report_path,
        commit=True,
        runs_dir=tmp_path / "runs",
        trash_fn=trash_fn,
    )
    assert result["committed"]
    assert result["moved"] == 2
    assert not discard_a.exists()
    assert not discard_b.exists()
    assert keeper_a.exists()
    assert keeper_b.exists()


def test_image_near_dup_group_validated_against_scan_roots(tmp_path: Path) -> None:
    """A near-dup member path outside report.roots is refused by the mover."""
    keeper = tmp_path / "live" / "orig.png"
    # Discard outside the roots umbrella.
    outside_root = tmp_path / "other"
    outside_root.mkdir(parents=True, exist_ok=True)
    discard = outside_root / "reencoded.jpg"
    keeper.parent.mkdir(parents=True, exist_ok=True)
    keeper.write_bytes(b"kb" * 500)
    discard.write_bytes(b"db" * 500)

    ka, da = keeper.stat(), discard.stat()

    report = Report(
        roots=[tmp_path / "live"],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=da.st_size,
        groups=[],
        image_near_dup_groups=[
            ReportGroup(
                id="image-near-dup-0000",
                kind="image-near-dup",
                hash="image-near-dup-0000",
                size=ka.st_size,
                reclaim_bytes=da.st_size,
                members=[
                    ReportMember(
                        path=keeper, size=ka.st_size, mtime=ka.st_mtime,
                        hash="KA", score=0.0, signals=[], is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=discard, size=da.st_size, mtime=da.st_mtime,
                        hash="DA", score=-1.0, signals=[], is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json())

    import pytest

    from duplicate_cleaner.apply.mover import ApplyError

    with pytest.raises(ApplyError):
        apply_report(report_path, commit=False, runs_dir=tmp_path / "runs")
