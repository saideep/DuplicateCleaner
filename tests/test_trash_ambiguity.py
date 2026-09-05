"""G5: ``_default_trash_fn`` returns ``None`` on ambiguity; caller respects it.

Prior code returned ``trash / path.name`` on ambiguity — a specific-but-
possibly-wrong path — and the caller wrote ``str(dest)`` unconditionally
into ``trashed_at_path``. The manifest then claimed authority for a guess
that the undo path would trust without verification.

G5 makes ``_default_trash_fn`` return ``None`` on any 0-or->1 diff, and
the caller only stamps a ``trashed_at_path`` when the return value is
``Path``. Undo's fallback (G4) then locates the file by basename+hash.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from duplicate_cleaner.apply import trash as trash_mod
from duplicate_cleaner.apply.mover import apply_report
from duplicate_cleaner.apply.trash import default_trash_fn as _default_trash_fn
from duplicate_cleaner.report.schema import Report, ReportGroup, ReportMember


def test_default_trash_fn_returns_none_on_zero_diff(tmp_path: Path) -> None:
    """Simulate a send2trash that does not create a new entry (no-op)."""
    trash = tmp_path / "trash"
    trash.mkdir()

    def fake_trash_dir_for(p: Path) -> Path:
        return trash

    class _NoopSend:
        @staticmethod
        def send2trash(_path: str) -> None:
            pass  # no filesystem side effect

    with patch.object(trash_mod, "trash_dir_for", fake_trash_dir_for), patch.object(
        trash_mod, "send2trash", _NoopSend
    ):
        # An arbitrary source path — no side effects from send2trash means
        # ``after - before`` is empty, so we're in the ambiguity branch.
        result = _default_trash_fn(tmp_path / "some.txt")
    assert result is None


def test_default_trash_fn_returns_none_on_multi_new(tmp_path: Path) -> None:
    """Two new files appear in Trash between snapshots — ambiguous."""
    trash = tmp_path / "trash"
    trash.mkdir()

    def fake_trash_dir_for(_p: Path) -> Path:
        return trash

    class _MultiSend:
        @staticmethod
        def send2trash(_path: str) -> None:
            (trash / "landed_a.txt").write_bytes(b"x")
            (trash / "landed_b.txt").write_bytes(b"y")

    with patch.object(trash_mod, "trash_dir_for", fake_trash_dir_for), patch.object(
        trash_mod, "send2trash", _MultiSend
    ):
        result = _default_trash_fn(tmp_path / "some.txt")
    assert result is None


def test_apply_leaves_manifest_trashed_at_none_when_trash_fn_returns_none(
    tmp_path: Path,
) -> None:
    """G5 caller contract: an ambiguous return leaves ``trashed_at_path=None``
    in the manifest so undo takes the (safer, hash-verified) fallback path.
    """
    a = tmp_path / "keep.txt"
    b = tmp_path / "discard.txt"
    a.write_bytes(b"data")
    b.write_bytes(b"data")

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
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json())

    ambiguous_trash = tmp_path / "trash"
    ambiguous_trash.mkdir()

    def trash_fn_none(p: Path) -> Path | None:
        # Simulate a real move that lost the destination path.
        import shutil

        shutil.move(str(p), str(ambiguous_trash / p.name))
        return None

    result = apply_report(
        report_path,
        commit=True,
        runs_dir=tmp_path / "runs",
        trash_fn=trash_fn_none,
    )
    assert result["moved"] == 1
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    (entry,) = manifest["entries"]
    assert entry["trashed_at_path"] is None
