"""Tests for the v0.3-b organize undo pipeline (undo.py)."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

from duplicate_cleaner.config import Config
from duplicate_cleaner.organize.mover import apply_plan
from duplicate_cleaner.organize.plan import PlanEntry, PlanFile, dest_for
from duplicate_cleaner.organize.undo import (
    restore_from_organize_manifest,
)


def _mk_cfg(tmp_root: Path) -> Config:
    return Config.model_validate({
        "active_homes": [tmp_root],
        "min_size_bytes": 1,
        "exclude_globs": [],
        "follow_symlinks": False,
    })


def _mk_entry(src_path: Path, *, domain: str = "HR",
              subfolder: str = "Payslips/2024") -> PlanEntry:
    st = src_path.stat()
    return PlanEntry(
        source_id="local",
        source_path=src_path,
        proposed_dest=dest_for(domain, subfolder, src_path.name),
        domain=domain,
        subfolder=subfolder,
        filename=src_path.name,
        size=st.st_size,
        mtime=st.st_mtime,
        confidence=0.9,
    )


def _mk_plan(tmp_path: Path, entries: list[PlanEntry],
             dest_root: Path | None = None) -> Path:
    plan = PlanFile(
        sources=["local"],
        roots=[tmp_path],
        dest_root=dest_root if dest_root is not None else (tmp_path / "organized"),
        total_files=len(entries),
        total_by_domain={},
        entries=entries,
        cohesion_groups=[],
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    return plan_path


def _mk_source(tmp_path: Path, name: str = "sample.pdf",
               body: bytes = b"sample-bytes") -> Path:
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    p = src_dir / name
    p.write_bytes(body)
    return p


# --------------------------------------------------------------------------- #
# Restore round-trip                                                          #
# --------------------------------------------------------------------------- #


def test_organize_undo_restores_moved_file(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="restore-me.pdf", body=b"undoable-bytes")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)
    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert result.moved == 1
    dest = tmp_path / "organized" / "HR" / "Payslips" / "2024" / "restore-me.pdf"
    assert dest.exists()
    assert not src.exists()

    undo_result = restore_from_organize_manifest(Path(result.manifest_path))  # type: ignore[arg-type]
    assert undo_result.restored == 1
    assert undo_result.errors == []
    assert src.exists()
    assert src.read_bytes() == b"undoable-bytes"
    assert not dest.exists()


# --------------------------------------------------------------------------- #
# Safety rails                                                                #
# --------------------------------------------------------------------------- #


def test_organize_undo_rejects_excluded_original_path(tmp_path: Path) -> None:
    """A poisoned manifest naming ~/Library/foo as source_path must be refused."""
    dest_real = tmp_path / "trash-src" / "victim.pdf"
    dest_real.parent.mkdir()
    dest_real.write_bytes(b"dest-payload")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "version": "0.3.0",
        "kind": "organize",
        "created_at": "20260906T000000Z",
        "dest_root": str(tmp_path / "organized"),
        "roots": [str(tmp_path)],
        "entries": [
            {
                "source_path": str(Path.home() / "Library" / "Preferences" / "poison.pdf"),
                "dest_path": str(dest_real),
                "size": 12,
                "mtime": 1_700_000_000.0,
                "cohesion_group_id": None,
                "cross_volume": False,
                "ts": "2026-09-06T00:00:00+00:00",
            },
        ],
        "collisions": [],
    }))

    result = restore_from_organize_manifest(manifest_path)
    assert result.restored == 0
    assert len(result.errors) == 1
    assert "excluded" in result.errors[0].lower() or "library" in result.errors[0].lower()
    # Dest untouched — no restore happened.
    assert dest_real.exists()


def test_organize_undo_missing_dest_logs_and_continues(tmp_path: Path) -> None:
    """User deleted the moved file after apply — undo logs, does not crash."""
    src_a = _mk_source(tmp_path, name="a.pdf", body=b"aaa")
    src_b = _mk_source(tmp_path, name="b.pdf", body=b"bbb")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src_a), _mk_entry(src_b)])
    cfg = _mk_cfg(tmp_path)
    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    dest_a = tmp_path / "organized" / "HR" / "Payslips" / "2024" / "a.pdf"
    dest_b = tmp_path / "organized" / "HR" / "Payslips" / "2024" / "b.pdf"
    assert dest_a.exists() and dest_b.exists()

    # User "deletes" b — simulate by moving out of the way (we can't use
    # os.unlink under the forbidden-calls rule, but we can send2trash it).
    import send2trash as _s2t
    _s2t.send2trash(str(dest_b))
    assert not dest_b.exists()

    undo_result = restore_from_organize_manifest(Path(result.manifest_path))  # type: ignore[arg-type]
    assert undo_result.restored == 1
    assert len(undo_result.errors) == 1
    assert "missing" in undo_result.errors[0].lower()
    # A was restored, B remains missing at source.
    assert src_a.exists()
    assert not src_b.exists()


def test_organize_undo_removes_empty_parent_dirs(tmp_path: Path) -> None:
    """Empty dest-side parent dirs are cleaned up post-restore."""
    src = _mk_source(tmp_path, name="doc.pdf", body=b"data")
    plan_path = _mk_plan(
        tmp_path,
        [_mk_entry(src, domain="Deep", subfolder="a/b/c")],
    )
    cfg = _mk_cfg(tmp_path)
    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    dest_leaf = tmp_path / "organized" / "Deep" / "a" / "b" / "c" / "doc.pdf"
    assert dest_leaf.exists()

    # Track calls to send2trash from inside the undo cleanup — verify the
    # cleanup walker at least reached one of the empty parent dirs.  Whether
    # send2trash succeeds in the pytest temp env is OS-dependent; the
    # invariant we care about is that the walker fires.
    cleanup_calls: list[str] = []

    real_send = __import__("send2trash").send2trash

    def _spy(target: str) -> None:
        cleanup_calls.append(target)
        with contextlib.suppress(OSError):
            real_send(target)

    with patch("send2trash.send2trash", side_effect=_spy):
        undo_result = restore_from_organize_manifest(Path(result.manifest_path))  # type: ignore[arg-type]

    assert undo_result.restored == 1
    # The cleanup walker attempted to remove at least one empty parent.
    assert cleanup_calls, "expected the empty-parent cleanup walker to fire"
    # Source is back.
    assert src.exists()


def test_organize_undo_atomic_operations(tmp_path: Path) -> None:
    """When shutil.move fails mid-batch, partial restore + remaining errors."""
    src_a = _mk_source(tmp_path, name="a.pdf", body=b"aaa")
    src_b = _mk_source(tmp_path, name="b.pdf", body=b"bbb")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src_a), _mk_entry(src_b)])
    cfg = _mk_cfg(tmp_path)
    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert result.moved == 2

    real_move = __import__("shutil").move
    calls = {"n": 0}

    def _flaky_move(src: str, dst: str, *a: object, **kw: object) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated mid-batch move failure")
        return str(real_move(src, dst, *a, **kw))

    with patch("duplicate_cleaner.organize.undo.shutil.move", side_effect=_flaky_move):
        undo_result = restore_from_organize_manifest(Path(result.manifest_path))  # type: ignore[arg-type]

    # One restored; one error.
    assert undo_result.restored == 1
    assert len(undo_result.errors) == 1
    assert "simulated" in undo_result.errors[0]


# --------------------------------------------------------------------------- #
# Regression checks                                                           #
# --------------------------------------------------------------------------- #


def test_organize_undo_reports_total(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="doc.pdf", body=b"content")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)
    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    undo_result = restore_from_organize_manifest(Path(result.manifest_path))  # type: ignore[arg-type]
    assert undo_result.total == 1
    assert undo_result.restored == 1


def test_organize_undo_refuses_to_overwrite_existing_source(tmp_path: Path) -> None:
    """If original source_path already exists on disk, skip the entry."""
    src = _mk_source(tmp_path, name="doc.pdf", body=b"content")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)
    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    # Recreate the source path — user pasted a new file at the same name.
    src.write_bytes(b"user-put-something-back-here")

    undo_result = restore_from_organize_manifest(Path(result.manifest_path))  # type: ignore[arg-type]
    assert undo_result.restored == 0
    assert len(undo_result.errors) == 1
    assert "already exists" in undo_result.errors[0].lower()
    # User's fresh file is unchanged.
    assert src.read_bytes() == b"user-put-something-back-here"
