"""End-to-end: build fixtures → scan → apply dry-run → apply --commit → undo.

Uses the corpus in ``tests/fixtures/build.py``. The trash step is stubbed
with a ``trash_fn`` that moves into a test-controlled directory so the real
``~/.Trash`` is never touched.

Requires ``blake3`` and ``send2trash`` on ``PYTHONPATH`` (imported by the
hashing pipeline and by ``apply.mover``). ``send2trash`` is imported at
module level in ``mover.py`` but is only invoked when ``trash_fn`` is
``None``; passing our own ``trash_fn`` keeps the real Trash out of the loop.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from duplicate_cleaner.apply.mover import apply_report
from duplicate_cleaner.apply.undo import restore_from_manifest
from duplicate_cleaner.compare.exact import group_by_hash
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import hash_records
from duplicate_cleaner.report.render import render_report
from duplicate_cleaner.report.schema import (
    Report,
    ReportGroup,
    ReportMember,
    ReportSignal,
)
from duplicate_cleaner.scan.walk import iter_files
from duplicate_cleaner.score.rules import score_groups
from duplicate_cleaner.store import Store
from tests.fixtures.build import build_corpus


def _run_scan(root: Path, active_home: Path, report_dir: Path) -> Path:
    """Emulate `dc scan` end-to-end and return the report.json path."""
    store = Store(report_dir / "cache.db")
    cfg = Config(active_homes=[active_home], min_size_bytes=0)
    file_count = 0

    def _count() -> object:
        nonlocal file_count
        for rec in iter_files([root], min_size_bytes=cfg.min_size_bytes):
            file_count += 1
            yield rec

    hashed = hash_records(_count(), store)
    groups = list(group_by_hash(hashed, min_size_bytes=cfg.min_size_bytes))
    scored = score_groups(groups, cfg, DEFAULT_WEIGHTS)

    report_groups: list[ReportGroup] = []
    total_reclaim = 0
    for sg in scored:
        members = [
            ReportMember(
                path=m.path,
                size=m.size,
                mtime=m.mtime,
                hash=m.hash,
                score=m.score,
                signals=[
                    ReportSignal(name=n, contribution=c) for n, c in m.signals
                ],
                is_proposed_keeper=m.is_proposed_keeper,
                is_informational=m.is_informational,
            )
            for m in sg.members
        ]
        report_groups.append(
            ReportGroup(
                id=sg.id,
                hash=sg.hash,
                size=sg.size,
                reclaim_bytes=sg.reclaim_bytes,
                members=members,
            )
        )
        total_reclaim += sg.reclaim_bytes

    report = Report(
        roots=[root.resolve()],
        total_files_scanned=file_count,
        total_groups=len(report_groups),
        total_reclaim_bytes=total_reclaim,
        groups=report_groups,
    )
    _, json_path = render_report(report, report_dir)
    store.close()
    return json_path


def test_full_pipeline_scan_apply_undo(tmp_path: Path) -> None:
    """Scan the fixture corpus, apply, then undo — files round-trip exactly."""
    corpus = tmp_path / "corpus"
    layout = build_corpus(corpus, include_git_repo=False)
    active_home = layout.exact_live.parent  # "live/"

    report_dir = tmp_path / "report"
    json_path = _run_scan(corpus, active_home, report_dir)
    assert json_path.exists()

    # ------------------------------------------------------------------
    # Scanned report content: the exact-duplicate pair must be grouped;
    # excluded dirs must NOT appear; the same-size / different-content
    # pair must NOT be grouped (they differ on full hash).
    # ------------------------------------------------------------------
    from duplicate_cleaner.apply.mover import load_report

    report = load_report(json_path)
    all_paths = {str(m.path) for g in report.groups for m in g.members}
    assert str(layout.exact_live.resolve()) in all_paths
    assert str(layout.exact_backup.resolve()) in all_paths
    assert str(layout.head_tail_a.resolve()) in all_paths
    assert str(layout.head_tail_b.resolve()) in all_paths
    # C differs in tail — must not appear in any exact-duplicate group.
    assert str(layout.head_tail_c_different_tail.resolve()) not in all_paths
    # Same-size / different-bytes pair must NOT group.
    assert str(layout.same_size_a.resolve()) not in all_paths
    assert str(layout.same_size_b.resolve()) not in all_paths
    # Excluded content must be invisible.
    for excluded in (
        layout.git_object_file,
        layout.node_modules_file,
        layout.icloud_placeholder,
    ):
        assert str(excluded.resolve()) not in all_paths

    # Locate the notes.txt group and confirm the backup copy is the proposed discard.
    notes_group = next(
        g for g in report.groups
        if any(Path(m.path).name == "notes.txt" for m in g.members)
    )
    keeper = next(m for m in notes_group.members if m.is_proposed_keeper)
    discards = [m for m in notes_group.members if not m.is_proposed_keeper]
    assert Path(keeper.path) == layout.exact_live.resolve()
    assert [Path(d.path) for d in discards] == [layout.exact_backup.resolve()]

    # ------------------------------------------------------------------
    # Dry-run apply — no state should change on disk.
    # ------------------------------------------------------------------
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    moved: list[Path] = []

    def trash_fn(p: Path) -> Path:
        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        moved.append(dest)
        return dest

    runs_dir = tmp_path / "runs"
    dry = apply_report(json_path, commit=False, runs_dir=runs_dir, trash_fn=trash_fn)
    assert dry["committed"] is False
    assert dry["moved"] == 0
    assert dry["manifest_path"] is None
    # Nothing on disk changed.
    assert layout.exact_backup.exists()
    assert layout.exact_live.exists()
    assert list(fake_trash.iterdir()) == []

    # ------------------------------------------------------------------
    # Real apply — discard files move to the fake trash.
    # ------------------------------------------------------------------
    committed = apply_report(
        json_path, commit=True, runs_dir=runs_dir, trash_fn=trash_fn
    )
    assert committed["committed"] is True
    assert committed["moved"] == dry["planned"]
    assert committed["moved"] >= 2  # notes.txt + one of the head+tail pair at least
    manifest_path = Path(committed["manifest_path"])
    assert manifest_path.exists()

    # Discarded paths are gone; keeper paths remain.
    assert not layout.exact_backup.exists()
    assert layout.exact_live.exists()
    # And the discard body sits in the fake trash.
    assert (fake_trash / layout.exact_backup.name).exists()

    # ------------------------------------------------------------------
    # Undo — every discarded file comes back with identical bytes.
    # ------------------------------------------------------------------
    original_backup_bytes = (fake_trash / layout.exact_backup.name).read_bytes()
    undo_result = restore_from_manifest(
        manifest_path,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert undo_result["errors"] == []
    assert undo_result["restored"] == committed["moved"]
    assert layout.exact_backup.exists()
    assert layout.exact_backup.read_bytes() == original_backup_bytes
