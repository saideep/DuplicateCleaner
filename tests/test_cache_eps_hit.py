"""G9: ``get_cached_hash`` uses the same eps rule as ``upsert_file``.

Prior code matched ``mtime = ?`` exactly for reads but used
``ABS(mtime - ?) <= 1e-6`` for writes. On filesystems where SQLite REAL
round-trip loses precision, reads returned miss even when the stored row
matched the on-disk stat within tolerance. Correctness was safe (rehash
returns the same result) but cache-hit rate collapsed silently.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from duplicate_cleaner.store import Store


def test_get_cached_hash_returns_hit_within_mtime_eps(tmp_path: Path) -> None:
    """Store an entry with mtime X, read back with mtime X + 5e-7. The eps
    tolerance is 1e-6, so this must hit.
    """
    store = Store(tmp_path / "cache.db")
    path = tmp_path / "target.bin"
    path.write_bytes(b"data")

    stored_mtime = 1_000_000.5
    read_mtime = stored_mtime + 5e-7  # inside the 1e-6 eps window

    store.upsert_file(
        path,
        size=4,
        mtime=stored_mtime,
        inode=42,
        dev=1,
        partial_hash="deadbeef" * 4,
        full_hash="cafef00d" * 8,
    )
    partial, full = store.get_cached_hash(path, 4, read_mtime)
    assert partial == "deadbeef" * 4
    assert full == "cafef00d" * 8
    store.close()


def test_get_cached_hash_misses_beyond_eps(tmp_path: Path) -> None:
    """A meaningful mtime change (well beyond eps) must miss."""
    store = Store(tmp_path / "cache.db")
    path = tmp_path / "target.bin"
    path.write_bytes(b"data")

    store.upsert_file(
        path,
        size=4,
        mtime=1_000_000.0,
        inode=42,
        dev=1,
        partial_hash="a" * 32,
        full_hash="b" * 64,
    )
    # 1 second later — real edit, must invalidate.
    partial, full = store.get_cached_hash(path, 4, 1_000_001.0)
    assert partial is None
    assert full is None
    store.close()


def test_get_cached_hash_eps_matches_upsert_eps(tmp_path: Path) -> None:
    """Round-trip regression: a hash upserted with SQLite REAL rounding is
    still readable by ``get_cached_hash`` (this was the original silent bug).
    """
    store = Store(tmp_path / "cache.db")
    path = tmp_path / "float.bin"
    path.write_bytes(b"data")

    # Insert directly with a slightly-perturbed mtime that would trip an
    # exact match. This simulates SQLite REAL round-trip loss.
    original_mtime = 1_700_000_000.1234567
    stored_mtime = 1_700_000_000.1234566  # differs in the last decimal only

    store._conn.execute(
        "INSERT INTO files (path, size, mtime, inode, dev, partial_hash, "
        "full_hash, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(path), 4, stored_mtime, 42, 1, "p" * 32, "f" * 64, 0.0),
    )
    store._conn.commit()

    partial, full = store.get_cached_hash(path, 4, original_mtime)
    assert partial == "p" * 32, "eps read must succeed for round-tripped floats"
    assert full == "f" * 64
    store.close()


def test_migrations_drop_legacy_scan_stage(tmp_path: Path) -> None:
    """G3 migration: an old cache DB with the pre-scan_id ``scan_stage`` table
    is auto-migrated. Opening a new Store must not raise.
    """
    db = tmp_path / "cache.db"
    # Simulate a pre-G3 schema.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE scan_stage (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            inode INTEGER,
            dev INTEGER,
            nlink INTEGER
        );
        INSERT INTO scan_stage VALUES ('/tmp/foo', 4, 0.0, 1, 1, 1);
        """
    )
    conn.commit()
    conn.close()

    # Opening the Store should migrate the schema — new columns present.
    store = Store(db)
    cols = [
        r["name"]
        for r in store._conn.execute("PRAGMA table_info(scan_stage)").fetchall()
    ]
    assert "scan_id" in cols
    assert "staged_ts" in cols
    store.close()
