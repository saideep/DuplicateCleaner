from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.compare.exact import Group
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.score.rules import score_group


def _hr(path: Path, *, size: int = 100, mtime: float = 1000.0) -> HashedRecord:
    return HashedRecord(
        path=path,
        size=size,
        mtime=mtime,
        inode=abs(hash(str(path))) & 0xFFFFFFFF,
        dev=1,
        nlink=1,
        full_hash="H" * 64,
    )


def test_active_home_beats_backup_home(tmp_path: Path) -> None:
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    (tmp_path / "Volumes" / "OldBackup" / "Users" / "me" / "Documents").mkdir(
        parents=True
    )

    p_active = active / "Documents" / "foo.txt"
    p_backup = (
        tmp_path / "Volumes" / "OldBackup" / "Users" / "me" / "Documents" / "foo.txt"
    )
    p_active.write_bytes(b"x")
    p_backup.write_bytes(b"x")

    group = Group(hash="H", size=1, members=[_hr(p_active), _hr(p_backup)])
    cfg = Config(active_homes=[active])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[p_active].is_proposed_keeper
    assert not by_path[p_backup].is_proposed_keeper


def test_backup_folder_marker_penalised(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    (home / "Documents").mkdir(parents=True)
    live = home / "Documents" / "foo.txt"
    live.write_bytes(b"x")
    (home / "Documents" / "backup").mkdir()
    backup = home / "Documents" / "backup" / "foo.txt"
    backup.write_bytes(b"x")

    group = Group(hash="H", size=1, members=[_hr(live), _hr(backup)])
    cfg = Config(active_homes=[home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[live].is_proposed_keeper
    # And the backup member should carry the path-marker signal.
    assert any("path marker" in s[0] for s in by_path[backup].signals)


def test_numbered_copy_filename_penalised(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    original = home / "foo.txt"
    numbered = home / "foo (1).txt"
    original.write_bytes(b"x")
    numbered.write_bytes(b"x")

    group = Group(hash="H", size=1, members=[_hr(original), _hr(numbered)])
    cfg = Config(active_homes=[home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[original].is_proposed_keeper
    assert any("copy marker" in s[0] for s in by_path[numbered].signals)


def test_deeper_path_penalty(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "sub" / "sub2").mkdir(parents=True)
    shallow = home / "Documents" / "foo.txt"
    deep = home / "Documents" / "sub" / "sub2" / "foo.txt"
    shallow.write_bytes(b"x")
    deep.write_bytes(b"x")

    group = Group(hash="H", size=1, members=[_hr(shallow), _hr(deep)])
    cfg = Config(active_homes=[home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[shallow].score > by_path[deep].score
    assert by_path[shallow].is_proposed_keeper


def test_newest_mtime_beats_older(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    older = home / "a.txt"
    newer = home / "b.txt"
    older.write_bytes(b"x")
    newer.write_bytes(b"x")

    group = Group(
        hash="H",
        size=1,
        members=[_hr(older, mtime=1000.0), _hr(newer, mtime=2000.0)],
    )
    cfg = Config(active_homes=[home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[newer].is_proposed_keeper


def test_hardlink_marked_informational(tmp_path: Path) -> None:
    """A group of two hardlinks: BOTH informational, NO keeper — trashing one
    would leave the inode alive under the other name, reclaim is zero."""
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    a = home / "a.txt"
    b = home / "b.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    # Force same inode/dev to simulate hard link.
    rec_a = HashedRecord(
        path=a, size=1, mtime=1000.0, inode=42, dev=1, nlink=2, full_hash="H"
    )
    rec_b = HashedRecord(
        path=b, size=1, mtime=1000.0, inode=42, dev=1, nlink=2, full_hash="H"
    )
    group = Group(hash="H", size=1, members=[rec_a, rec_b])
    cfg = Config(active_homes=[home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    info = [m for m in members if m.is_informational]
    keepers = [m for m in members if m.is_proposed_keeper]
    assert len(info) == 2
    assert len(keepers) == 0


def test_hardlink_pair_plus_separate_copy(tmp_path: Path) -> None:
    """Three-member group: two hardlinks (informational) plus a separate copy
    that becomes the keeper — reclaim comes only from the non-linked copy."""
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    a = home / "a.txt"
    b = home / "b.txt"
    c = home / "c.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    c.write_bytes(b"x")

    rec_a = HashedRecord(
        path=a, size=1, mtime=1000.0, inode=42, dev=1, nlink=2, full_hash="H"
    )
    rec_b = HashedRecord(
        path=b, size=1, mtime=1000.0, inode=42, dev=1, nlink=2, full_hash="H"
    )
    rec_c = HashedRecord(
        path=c, size=1, mtime=1000.0, inode=99, dev=1, nlink=1, full_hash="H"
    )
    group = Group(hash="H", size=1, members=[rec_a, rec_b, rec_c])
    cfg = Config(active_homes=[home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[a].is_informational
    assert by_path[b].is_informational
    assert not by_path[c].is_informational
    # The only non-linked member is the sole eligible keeper.
    assert by_path[c].is_proposed_keeper


def test_symlinked_home_matches_active_home(tmp_path: Path) -> None:
    """When active_homes is declared via a symlink, files under the resolved
    real directory must be recognized as 'under active home'.

    Two DIFFERENT physical files (distinct inodes) both under the same
    real tree; the active_home is declared as the symlinked alias. The
    scorer must resolve the symlink and match both files to the resolved
    active home.
    """
    import os as _os

    real_home = tmp_path / "real_home"
    (real_home / "Documents").mkdir(parents=True)
    a = real_home / "Documents" / "a.txt"
    b = real_home / "Documents" / "b.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    # A symlinked alias for the real home tree.
    linked_home = tmp_path / "linked_home"
    _os.symlink(real_home, linked_home)

    # Distinct inodes so the hardlink-informational rule does not fire —
    # this test is about symlink-resolution, not hardlink handling.
    rec_a = HashedRecord(
        path=a, size=1, mtime=1000.0, inode=101, dev=1, nlink=1, full_hash="H"
    )
    rec_b = HashedRecord(
        path=b, size=1, mtime=2000.0, inode=102, dev=1, nlink=1, full_hash="H"
    )
    group = Group(hash="H", size=1, members=[rec_a, rec_b])
    # Declare active_home via the symlinked alias — Config resolves it
    # to the real path.
    cfg = Config(active_homes=[linked_home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    # Both members should carry the +4 "under active home" signal.
    for m in members:
        assert any("under active home" in s[0] for s in m.signals), (
            f"member {m.path} did not match resolved active_home"
        )
