"""G3: two concurrent scans sharing one cache DB must not clobber each other.

Prior to G3 the ``scan_stage`` table was a single unnamespaced buffer that
each ``hash_records`` call cleared at startup. Two ``dc scan`` invocations
running against the same ``~/.cache/duplicate_cleaner/cache.db`` would
silently under-cluster whichever scan started second because the first
scan's staged rows had already been wiped.

The fix introduces a ``scan_id`` column; every read and write is scoped to
that id, and each scan cleans up only its own rows.
"""
from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.hash.pipeline import hash_records
from duplicate_cleaner.scan.walk import iter_files
from duplicate_cleaner.store import Store


def test_concurrent_scans_do_not_wipe_each_others_stage(tmp_path: Path) -> None:
    """Interleave two ``hash_records`` iterators against the same Store —
    each must still see its own size-bucketed groups after the other one
    has walked and staged its own rows."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    # Each root contains a duplicate pair with a unique-to-that-root size
    # so we can tell whose stage rows drove which output.
    (root_a / "x1.bin").write_bytes(b"A" * 4096)
    (root_a / "x2.bin").write_bytes(b"A" * 4096)
    (root_b / "y1.bin").write_bytes(b"B" * 8192)
    (root_b / "y2.bin").write_bytes(b"B" * 8192)

    store = Store(tmp_path / "cache.db")

    # Kick off both scans. Interleave by consuming the first record of one,
    # then draining the other, then finishing the first. This exercises the
    # race the fix targets: two active scans sharing the DB.
    it_a = hash_records(iter_files([root_a]), store)
    it_b = hash_records(iter_files([root_b]), store)

    first_a = next(it_a)
    b_results = list(it_b)
    rest_a = list(it_a)

    a_results = [first_a, *rest_a]
    store.close()

    a_paths = {r.path.name for r in a_results}
    b_paths = {r.path.name for r in b_results}
    # Neither scan lost its rows to the other.
    assert a_paths == {"x1.bin", "x2.bin"}
    assert b_paths == {"y1.bin", "y2.bin"}


def test_stage_ttl_sweeps_leaked_rows(tmp_path: Path) -> None:
    """Rows staged in a prior crashed scan are swept by
    ``sweep_stale_scan_stage`` at the start of the next ``hash_records``."""
    store = Store(tmp_path / "cache.db")

    # Insert a row directly with an ancient staged_ts (simulate a leaked row
    # from a crashed scan 48h ago).
    store._conn.execute(
        "INSERT INTO scan_stage "
        "(scan_id, path, size, mtime, inode, dev, nlink, staged_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("stale-scan-uuid", "/tmp/dead.bin", 100, 1000.0, 1, 1, 1, 0.0),
    )
    store.commit()
    (count_before,) = store._conn.execute(
        "SELECT COUNT(*) FROM scan_stage"
    ).fetchone()
    assert count_before == 1

    swept = store.sweep_stale_scan_stage()
    assert swept == 1
    (count_after,) = store._conn.execute(
        "SELECT COUNT(*) FROM scan_stage"
    ).fetchone()
    assert count_after == 0
    store.close()
