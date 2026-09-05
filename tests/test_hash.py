from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.compare.exact import group_by_hash
from duplicate_cleaner.hash.pipeline import hash_records
from duplicate_cleaner.scan.walk import iter_files
from duplicate_cleaner.store import Store


def test_identical_files_group_and_diff_files_split(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello" * 2000)
    (tmp_path / "b.txt").write_bytes(b"hello" * 2000)
    # Same size as a/b (10000 bytes) but different content.
    (tmp_path / "c.txt").write_bytes(b"world" * 2000)

    store = Store(tmp_path / "cache.db")
    records = list(iter_files([tmp_path]))
    hashed = list(hash_records(records, store))
    groups = list(group_by_hash(hashed))
    store.close()

    assert len(groups) == 1
    g = groups[0]
    names = {m.path.name for m in g.members}
    assert names == {"a.txt", "b.txt"}


def test_cache_hits_on_rescan(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello" * 2000)
    (tmp_path / "b.txt").write_bytes(b"hello" * 2000)

    db_path = tmp_path / "cache.db"
    store1 = Store(db_path)
    r1 = list(iter_files([tmp_path]))
    _ = list(hash_records(r1, store1))
    stats1 = store1.cache_stats()
    store1.close()
    assert stats1["with_full_hash"] == 2

    # Second run should not add new rows; existing (path,size,mtime) hits cache.
    store2 = Store(db_path)
    r2 = list(iter_files([tmp_path]))
    _ = list(hash_records(r2, store2))
    stats2 = store2.cache_stats()
    store2.close()

    assert stats2["files"] == stats1["files"]
    assert stats2["with_full_hash"] == stats1["with_full_hash"]


def test_large_files_hash_correctly(tmp_path: Path) -> None:
    """Files > 128KB exercise the head+tail partial hash path."""
    size = 200 * 1024
    (tmp_path / "big1.bin").write_bytes(b"A" * size)
    (tmp_path / "big2.bin").write_bytes(b"A" * size)
    (tmp_path / "big3.bin").write_bytes(b"B" * size)

    store = Store(tmp_path / "cache.db")
    records = list(iter_files([tmp_path]))
    hashed = list(hash_records(records, store))
    groups = list(group_by_hash(hashed))
    store.close()

    assert len(groups) == 1
    names = {m.path.name for m in groups[0].members}
    assert names == {"big1.bin", "big2.bin"}


def test_partial_hash_uses_tail_not_just_head(tmp_path: Path) -> None:
    """F18: three same-size files, all with identical 64 KB heads, where two
    also share an identical 64 KB tail (and full body) and the third only
    diverges in the tail. The two should group; the third must NOT."""
    from duplicate_cleaner.hash.pipeline import PARTIAL_CHUNK

    # Body layout: head (identical) + middle (identical) + tail.
    # Files are large enough that partial hashing samples both head AND tail
    # (must be > PARTIAL_CHUNK * 2, per _hash_partial).
    body_size = PARTIAL_CHUNK * 4
    head = b"H" * PARTIAL_CHUNK
    middle = b"M" * (body_size - 2 * PARTIAL_CHUNK)
    tail_same = b"T" * PARTIAL_CHUNK
    tail_diff = b"X" * PARTIAL_CHUNK

    (tmp_path / "a.bin").write_bytes(head + middle + tail_same)
    (tmp_path / "b.bin").write_bytes(head + middle + tail_same)
    # Different tail — must not group with a/b, even though head matches.
    (tmp_path / "c.bin").write_bytes(head + middle + tail_diff)

    store = Store(tmp_path / "cache.db")
    records = list(iter_files([tmp_path]))
    hashed = list(hash_records(records, store))
    groups = list(group_by_hash(hashed))
    store.close()

    assert len(groups) == 1
    names = {m.path.name for m in groups[0].members}
    assert names == {"a.bin", "b.bin"}


def test_cache_invalidates_on_stat_change(tmp_path: Path) -> None:
    """F13: writing an existing path with a new (size, mtime) must wipe the
    stale partial_hash and full_hash — no COALESCE across a stat change."""
    from duplicate_cleaner.store import Store

    db = tmp_path / "cache.db"
    store = Store(db)
    path = tmp_path / "file.txt"
    path.write_bytes(b"original")

    # Seed with a completely fake pair of hashes.
    store.upsert_file(
        path,
        size=8,
        mtime=1000.0,
        inode=1,
        dev=1,
        partial_hash="P_stale",
        full_hash="F_stale",
    )

    # Same path, different stats, no new hashes supplied — the row must NOT
    # carry the old hashes forward.
    store.upsert_file(
        path,
        size=99,
        mtime=2000.0,
        inode=1,
        dev=1,
        partial_hash=None,
        full_hash=None,
    )
    partial, full = store.get_cached_hash(path, 99, 2000.0)
    assert partial is None
    assert full is None
    store.close()
