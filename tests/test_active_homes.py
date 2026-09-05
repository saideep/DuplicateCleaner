"""Multi-home / backup-home scoring — a real user pattern.

The user has multiple ``~/Documents``/``~/Desktop`` trees accumulated across
migrations. The tool must:

* score files under a declared active_home as +4 (kept)
* score files under a look-alike homey tree elsewhere as -4 (discarded)
* recognize a symlinked/firmlinked active home path
"""
from __future__ import annotations

import os
from pathlib import Path

from duplicate_cleaner.compare.exact import Group
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.score.rules import score_group


def _rec(path: Path, inode: int) -> HashedRecord:
    """One HashedRecord — inode is arbitrary but must differ across paths
    so scoring is not short-circuited by the hardlink-informational rule.
    """
    return HashedRecord(
        path=path,
        size=32,
        mtime=1000.0,
        inode=inode,
        dev=1,
        nlink=1,
        full_hash="H" * 64,
    )


def test_backup_home_scores_lower_than_live(tmp_path: Path) -> None:
    """Same file under ``live/vaannada/Documents/`` and under a
    ``backup/OldMac/Users/vaannada/Documents/`` tree — backup loses.
    """
    live_home = tmp_path / "live" / "vaannada"
    (live_home / "Documents").mkdir(parents=True)
    live = live_home / "Documents" / "report.pdf"
    live.write_bytes(b"data")

    backup_home = (
        tmp_path / "backup" / "OldMac" / "Users" / "vaannada"
    )
    (backup_home / "Documents").mkdir(parents=True)
    backup = backup_home / "Documents" / "report.pdf"
    backup.write_bytes(b"data")

    group = Group(hash="H", size=4, members=[_rec(live, 1), _rec(backup, 2)])
    cfg = Config(active_homes=[live_home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}

    assert by_path[live].is_proposed_keeper, "live copy must be kept"
    assert not by_path[backup].is_proposed_keeper, "backup copy must be discarded"

    # Score gap must reflect the active/inactive-home weights: +4 vs -4 = 8
    # minus tiny depth deltas. The live copy is deeper so we allow slop.
    assert by_path[live].score - by_path[backup].score >= 6
    assert any("under active home" in s[0] for s in by_path[live].signals)
    assert any(
        "archived/backup home" in s[0] for s in by_path[backup].signals
    )


def test_backup_home_scores_lower_when_active_home_is_a_symlink(
    tmp_path: Path,
) -> None:
    """Firmlinked-home scenario: active home is a symlink into the real tree.

    Config resolves the symlink; the scorer must still treat files sitting
    inside the *resolved* real tree as "under active home".
    """
    real_home = tmp_path / "real" / "vaannada"
    (real_home / "Documents").mkdir(parents=True)
    live = real_home / "Documents" / "notes.md"
    live.write_bytes(b"data")

    backup_home = tmp_path / "backup" / "OldMac" / "Users" / "vaannada"
    (backup_home / "Documents").mkdir(parents=True)
    backup = backup_home / "Documents" / "notes.md"
    backup.write_bytes(b"data")

    link_home = tmp_path / "live" / "vaannada"
    link_home.parent.mkdir(parents=True)
    os.symlink(real_home, link_home)

    group = Group(hash="H", size=4, members=[_rec(live, 1), _rec(backup, 2)])
    # Declare the SYMLINK as the active_home; Config resolves it to the real path.
    cfg = Config(active_homes=[link_home])
    members = score_group(group, cfg, DEFAULT_WEIGHTS)
    by_path = {m.path: m for m in members}
    assert by_path[live].is_proposed_keeper
    assert not by_path[backup].is_proposed_keeper
    assert any("under active home" in s[0] for s in by_path[live].signals)
