"""Cache invalidation: rewriting a file with new (size, mtime) recomputes hashes.

The cache is keyed on (path, size, mtime). When a file is rewritten with a
new size or mtime the cached partial/full hash is invalidated — a stale hash
must never be returned for the new content.
"""
from __future__ import annotations

import os
from pathlib import Path

from duplicate_cleaner.compare.exact import group_by_hash
from duplicate_cleaner.hash.pipeline import hash_records
from duplicate_cleaner.scan.walk import iter_files
from duplicate_cleaner.store import Store


def _scan(root: Path, db: Path) -> tuple[str, dict[str, int]]:
    """Run one pass and return (that path's full_hash, stats)."""
    store = Store(db)
    _ = list(hash_records(iter_files([root]), store))
    stats = store.cache_stats()
    row = store._conn.execute(
        "SELECT full_hash FROM files WHERE path LIKE '%target.bin'"
    ).fetchone()
    store.close()
    return (row["full_hash"] if row else ""), stats


def test_rewriting_file_forces_hash_recompute(tmp_path: Path) -> None:
    """Same path, new content and new size → new cache row, no stale hash."""
    target = tmp_path / "target.bin"
    sibling = tmp_path / "sibling.bin"

    original = b"AAAAA" * 4000  # 20 000 bytes
    target.write_bytes(original)
    sibling.write_bytes(original)  # forces a size-bucket so it gets hashed
    # Pin an mtime we control so the change below is unambiguous.
    os.utime(target, (1_000_000.0, 1_000_000.0))
    os.utime(sibling, (1_000_000.0, 1_000_000.0))

    db = tmp_path / "cache.db"
    first_hash, first_stats = _scan(tmp_path, db)
    assert first_hash, "target.bin must have been hashed on first scan"

    # Rewrite with different content AND different size.
    new_content = b"ZZZZZZ" * 5000  # 30 000 bytes — different size
    target.write_bytes(new_content)
    os.utime(target, (2_000_000.0, 2_000_000.0))
    # sibling must also change so the size-bucket for the new size has 2 members
    # (rescan pipeline only hashes size-buckets with >1 member).
    sibling.write_bytes(new_content)
    os.utime(sibling, (2_000_000.0, 2_000_000.0))

    second_hash, second_stats = _scan(tmp_path, db)
    assert second_hash, "target.bin must be re-hashed on second scan"
    assert second_hash != first_hash, (
        "cache returned a stale hash after (size, mtime) changed"
    )
    # Cache holds the current row for the path (path is PK); either the
    # count is unchanged (row replaced) or it grew — never dropped.
    assert second_stats["files"] >= first_stats["files"]


def test_cache_hit_when_stat_unchanged(tmp_path: Path) -> None:
    """A rescan with no filesystem changes must not add new hashed rows."""
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"XYZ" * 5000)
    b.write_bytes(b"XYZ" * 5000)

    db = tmp_path / "cache.db"
    _, stats1 = _scan(tmp_path, db)
    _, stats2 = _scan(tmp_path, db)
    assert stats1 == stats2, "cache-hit rescan should be a no-op on counts"


def test_size_bucket_prunes_singletons_no_hash(tmp_path: Path) -> None:
    """A size that only one file has must never trigger any full hashing."""
    (tmp_path / "unique.bin").write_bytes(b"unique-content-block")
    (tmp_path / "pair_a.bin").write_bytes(b"XX" * 200)
    (tmp_path / "pair_b.bin").write_bytes(b"XX" * 200)

    db = tmp_path / "cache.db"
    store = Store(db)
    hashed = list(hash_records(iter_files([tmp_path]), store))
    groups = list(group_by_hash(hashed))
    names_hashed = {h.path.name for h in hashed}
    store.close()

    assert "unique.bin" not in names_hashed, (
        "unique-size file must not be hashed"
    )
    assert names_hashed == {"pair_a.bin", "pair_b.bin"}
    assert len(groups) == 1
