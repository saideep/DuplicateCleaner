"""Mover + undo integration for project-tree discards (v0.4)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.apply.undo import restore_from_manifest
from duplicate_cleaner.report.schema import (
    Report,
    ReportGroup,
    ReportMember,
    TreeDiffEntry,
)


def _write_project_dir(root: Path, files: dict[str, bytes]) -> Path:
    """Create a project directory with a non-git marker + the given files.

    Uses ``Cargo.toml`` rather than a fabricated ``.git/HEAD`` so
    ``is_git_repo_dirty`` (fail-closed on any non-zero git exit) does not
    treat a hand-crafted .git shell as a dirty repo.  The dirty-git rail
    is covered by ``test_apply_tree_discard_refuses_dirty_git`` which
    uses a real ``git init``.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return root


def _mk_tree_report(
    tmp_path: Path,
    keeper: Path,
    discard: Path,
    total_bytes: int,
    identical: int = 5,
) -> Path:
    """Emit a report.json containing a single ``kind="tree"`` group.

    Uses the same tmp_path as the caller so `report.roots` is `tmp_path`.
    """
    report = Report(
        roots=[tmp_path],
        total_files_scanned=identical * 2,
        total_groups=1,
        total_reclaim_bytes=total_bytes,
        groups=[
            ReportGroup(
                id="tree-0000",
                kind="tree",
                size=total_bytes,
                hash="tree-0000",
                reclaim_bytes=total_bytes,
                identical_file_count=identical,
                tree_diff=[
                    TreeDiffEntry(relative_path="notes.txt", hashes_per_member=[
                        "aaaaaaaaaaaaaaaa",
                        "bbbbbbbbbbbbbbbb",
                    ])
                ],
                similarity_pct=95.0,
                members=[
                    ReportMember(
                        path=keeper,
                        size=total_bytes,
                        mtime=0.0,
                        hash="",
                        score=1.0,
                        signals=[],
                        is_proposed_keeper=True,
                        is_informational=False,
                    ),
                    ReportMember(
                        path=discard,
                        size=total_bytes,
                        mtime=0.0,
                        hash="",
                        score=-5.0,
                        signals=[],
                        is_proposed_keeper=False,
                        is_informational=False,
                    ),
                ],
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


def test_apply_tree_discard_moves_whole_directory(tmp_path: Path) -> None:
    """A tree-group discard sends the whole project directory to Trash."""
    keeper = _write_project_dir(
        tmp_path / "live" / "myproj",
        {"a.txt": b"aa", "b.txt": b"bb", "src/c.py": b"cc"},
    )
    discard = _write_project_dir(
        tmp_path / "live" / "backup" / "myproj",
        {"a.txt": b"aa", "b.txt": b"bb", "src/c.py": b"cc"},
    )
    total = sum(p.stat().st_size for p in discard.rglob("*") if p.is_file())
    report_path = _mk_tree_report(tmp_path, keeper, discard, total)

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    def trash_fn(p: Path) -> Path:
        # Emulate send2trash: move whole tree (file or dir) into fake_trash.
        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    runs = tmp_path / "runs"
    result = apply_report(
        report_path,
        commit=True,
        runs_dir=runs,
        trash_fn=trash_fn,
        active_homes=[tmp_path / "live"],
    )
    assert result["committed"]
    assert result["moved_tree"] == 1
    # Discard directory moved.
    assert not discard.exists()
    assert (fake_trash / discard.name).exists()
    assert (fake_trash / discard.name / "a.txt").exists()
    # Keeper untouched.
    assert keeper.exists()
    assert (keeper / "a.txt").read_bytes() == b"aa"


def test_apply_tree_discard_refuses_outside_active_homes(tmp_path: Path) -> None:
    """A tree discard outside active_homes is refused."""
    keeper = _write_project_dir(
        tmp_path / "live" / "myproj",
        {"a.txt": b"aa"},
    )
    # Discard OUTSIDE the active_homes umbrella but INSIDE report.roots.
    outside = _write_project_dir(
        tmp_path / "other" / "myproj",
        {"a.txt": b"aa"},
    )
    report_path = _mk_tree_report(tmp_path, keeper, outside, total_bytes=2)

    with pytest.raises(ApplyError) as excinfo:
        apply_report(
            report_path,
            commit=False,  # even dry-run runs validation
            runs_dir=tmp_path / "runs",
            active_homes=[tmp_path / "live"],
        )
    assert "active home" in str(excinfo.value).lower()


def test_apply_tree_discard_refuses_dirty_git(tmp_path: Path) -> None:
    """A discard whose git repo has uncommitted changes is refused."""
    import subprocess

    keeper = _write_project_dir(
        tmp_path / "live" / "myproj_clean",
        {"a.txt": b"aa"},
    )
    dirty = tmp_path / "live" / "myproj_dirty"
    dirty.mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(dirty)], check=True, env=env
    )
    (dirty / "a.txt").write_text("committed")
    subprocess.run(["git", "-C", str(dirty), "add", "a.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(dirty), "commit", "-q", "-m", "s"], check=True, env=env
    )
    # Add uncommitted work.
    (dirty / "uncommitted.txt").write_text("in-progress")

    report_path = _mk_tree_report(tmp_path, keeper, dirty, total_bytes=100)

    with pytest.raises(ApplyError) as excinfo:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            active_homes=[tmp_path / "live"],
        )
    assert "uncommitted" in str(excinfo.value).lower()


def test_undo_tree_restore_moves_directory_back(tmp_path: Path) -> None:
    """Apply + undo round-trip on a project directory."""
    keeper = _write_project_dir(
        tmp_path / "live" / "myproj_keeper",
        {"a.txt": b"aa"},
    )
    discard = _write_project_dir(
        tmp_path / "live" / "backup" / "myproj",
        {"a.txt": b"aa", "b.txt": b"bb"},
    )
    total = sum(p.stat().st_size for p in discard.rglob("*") if p.is_file())
    report_path = _mk_tree_report(tmp_path, keeper, discard, total)

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
        active_homes=[tmp_path / "live"],
    )
    manifest = Path(result["manifest_path"])
    assert not discard.exists()
    assert (fake_trash / discard.name).exists()

    undo = restore_from_manifest(manifest, allowed_trash_dirs=[fake_trash])
    assert undo["errors"] == [], undo["errors"]
    assert undo["restored_tree"] == 1
    # Directory is back with contents.
    assert discard.exists()
    assert (discard / "a.txt").read_bytes() == b"aa"
    assert (discard / "b.txt").read_bytes() == b"bb"


def test_apply_tree_discard_refuses_when_not_a_directory(tmp_path: Path) -> None:
    """Tree groups whose discard path is a file are refused."""
    keeper = _write_project_dir(tmp_path / "live" / "keep", {"a.txt": b"a"})
    not_a_dir = tmp_path / "live" / "wrong.txt"
    not_a_dir.write_text("not a directory")
    report_path = _mk_tree_report(tmp_path, keeper, not_a_dir, total_bytes=1)

    with pytest.raises(ApplyError) as excinfo:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            active_homes=[tmp_path / "live"],
        )
    assert "not a directory" in str(excinfo.value).lower()


def test_apply_tree_discard_refuses_when_active_homes_empty(tmp_path: Path) -> None:
    """K1 (audit pass 14 blocker): tree groups + active_homes=[] → refuse."""
    keeper = _write_project_dir(tmp_path / "live" / "myproj", {"a.txt": b"aa"})
    discard = _write_project_dir(tmp_path / "live" / "backup" / "myproj", {"a.txt": b"aa"})
    report_path = _mk_tree_report(tmp_path, keeper, discard, total_bytes=2)

    with pytest.raises(ApplyError) as excinfo:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            active_homes=[],
        )
    msg = str(excinfo.value).lower()
    assert "active_home" in msg or "active home" in msg


def test_undo_rejects_manifest_with_tree_flag_on_file_entry(tmp_path: Path) -> None:
    """K3 (audit pass 14 deferrable): hand-edited manifest that sets
    ``is_project_tree=True`` on an entry pointing at a regular file inside
    Trash must be refused — otherwise ``shutil.move`` would run against a
    directory-shaped code path targeting a file.
    """
    import json as _json

    from duplicate_cleaner.apply.undo import restore_from_manifest

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    # A regular FILE at the trashed_at_path — but the manifest lies and
    # claims it's a project tree.
    trashed_file = fake_trash / "not_a_dir.txt"
    trashed_file.write_text("payload")
    original = tmp_path / "restored_here"

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        _json.dumps(
            {
                "manifest_version": "0.2.0",
                "created_at": "20260907T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": 7,
                        "mtime": 0.0,
                        "hash": "",
                        "trashed_at_path": str(trashed_file),
                        "is_project_tree": True,
                    }
                ],
            }
        )
    )
    result = restore_from_manifest(
        manifest, allowed_trash_dirs=[fake_trash]
    )
    assert result["restored"] == 0
    assert not original.exists()
    # Trashed file untouched — no rogue shutil.move fired.
    assert trashed_file.exists()
    assert any("not a directory" in e.lower() for e in result["errors"]), result["errors"]


def test_apply_tree_discard_refuses_when_active_homes_none(tmp_path: Path) -> None:
    """K1 (audit pass 14 blocker): tree groups + active_homes=None → refuse."""
    keeper = _write_project_dir(tmp_path / "live" / "myproj", {"a.txt": b"aa"})
    discard = _write_project_dir(tmp_path / "live" / "backup" / "myproj", {"a.txt": b"aa"})
    report_path = _mk_tree_report(tmp_path, keeper, discard, total_bytes=2)

    with pytest.raises(ApplyError) as excinfo:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            active_homes=None,
        )
    msg = str(excinfo.value).lower()
    assert "active_home" in msg or "active home" in msg
