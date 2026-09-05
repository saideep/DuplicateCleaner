"""Hash reconciliation — cache hits, download budget, singleton skip, cross-algo download."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.hash.reconciliation import (
    ReconciliationBudget,
    make_budget,
    reconcile_bucket,
)
from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.store import Store


def _blake3(data: bytes) -> str:
    return str(blake3.blake3(data).hexdigest())


def _rec(
    *,
    source: str,
    size: int,
    foreign: str | None,
    cloud_id: str | None = None,
    etag: str | None = None,
    path: str | None = None,
) -> FileRecord:
    return FileRecord(
        path=Path(path or f"{source}://virtual/{cloud_id or 'local'}"),
        size=size,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id=source,
        foreign_hash=foreign,
        etag=etag,
        cloud_file_id=cloud_id,
    )


def test_singleton_bucket_returns_empty_no_download(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "cache.db")
    calls: list[FileRecord] = []

    def _reader(r: FileRecord) -> Iterator[bytes]:
        calls.append(r)
        yield b""

    result = reconcile_bucket(
        [_rec(source="gdrive:x", size=10, foreign="md5:aaa", cloud_id="a", etag="e")],
        store,
        _reader,
    )
    assert result == []
    assert calls == []
    store.close()


def test_shared_algo_bucket_uses_foreign_hash_and_skips_download(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "cache.db")
    calls: list[FileRecord] = []

    def _reader(r: FileRecord) -> Iterator[bytes]:
        calls.append(r)
        yield b""

    members = [
        _rec(
            source="gdrive:x",
            size=10,
            foreign="blake3:AAAA",
            cloud_id="a",
            etag="e1",
        ),
        _rec(
            source="local",
            size=10,
            foreign="blake3:AAAA",
        ),
    ]
    got = reconcile_bucket(members, store, _reader)
    assert got is not None
    assert calls == []  # zero downloads
    assert {r.blake3 for r in got} == {"AAAA"}
    store.close()


def test_cache_hit_skips_download(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "cache.db")
    store.put_cloud_hash("gdrive:x", "a", "etagA", "cached-hash", size=1000)
    store.put_cloud_hash("gdrive:x", "b", "etagB", "cached-hash", size=1000)

    calls: list[FileRecord] = []

    def _reader(r: FileRecord) -> Iterator[bytes]:
        calls.append(r)
        yield b""

    members = [
        _rec(
            source="gdrive:x",
            size=1000,
            foreign="md5:AAA",
            cloud_id="a",
            etag="etagA",
        ),
        _rec(
            source="gdrive:x",
            size=1000,
            foreign="md5:BBB",
            cloud_id="b",
            etag="etagB",
        ),
    ]
    got = reconcile_bucket(members, store, _reader)
    assert got is not None
    assert calls == []
    assert {r.blake3 for r in got} == {"cached-hash"}
    assert all(r.from_cache for r in got)
    store.close()


def test_cross_algo_triggers_download_and_populates_cache(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "cache.db")
    payload = b"hello-world-payload"

    def _reader(_r: FileRecord) -> Iterator[bytes]:
        yield payload

    cloud = _rec(
        source="gdrive:x",
        size=len(payload),
        foreign="md5:aabbcc",
        cloud_id="c1",
        etag="etag1",
    )
    local = _rec(
        source="local",
        size=len(payload),
        foreign="blake3:LOCAL_HASH",
    )
    # local_blake3_lookup returns the local blake3 so we don't re-hash locally.
    got = reconcile_bucket(
        [cloud, local],
        store,
        _reader,
        local_blake3_lookup=lambda _m: "LOCAL_HASH",
    )
    assert got is not None
    hashes = {r.record.source_id: r.blake3 for r in got}
    assert hashes["local"] == "LOCAL_HASH"
    assert hashes["gdrive:x"] == _blake3(payload)
    # And the cache has been populated for the cloud entry.
    cached = store.get_cloud_hash("gdrive:x", "c1", "etag1")
    assert cached == _blake3(payload)
    store.close()


def test_download_cap_defers_bucket(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "cache.db")
    payload = b"payload"

    def _reader(_r: FileRecord) -> Iterator[bytes]:
        yield payload

    cloud_big = _rec(
        source="gdrive:x",
        size=100 * 1024 * 1024,  # 100 MB
        foreign="md5:aa",
        cloud_id="big",
        etag="e",
    )
    local = _rec(source="local", size=100 * 1024 * 1024, foreign="blake3:L")
    budget = ReconciliationBudget(max_download_bytes=1024)  # 1 KiB cap
    got = reconcile_bucket(
        [cloud_big, local],
        store,
        _reader,
        budget=budget,
        local_blake3_lookup=lambda _m: "L",
    )
    assert got is None
    assert budget.deferred_buckets  # recorded
    assert store.get_cloud_hash("gdrive:x", "big", "e") is None
    store.close()


def test_make_budget_defaults_and_clamp() -> None:
    assert make_budget(None).max_download_bytes == (1000 * 1024 * 1024)
    assert make_budget(0).max_download_bytes == 0
    assert make_budget(-5).max_download_bytes == 0


def test_etag_change_invalidates_cache(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "cache.db")
    store.put_cloud_hash("gdrive:x", "id1", "old-etag", "OLD", size=10)
    assert store.get_cloud_hash("gdrive:x", "id1", "old-etag") == "OLD"
    assert store.get_cloud_hash("gdrive:x", "id1", "new-etag") is None
    store.close()


def test_purge_stale_cache_returns_row_count(tmp_path: Path) -> None:
    import time as _time

    store = Store(path=tmp_path / "cache.db")
    store.put_cloud_hash("gdrive:x", "a", "e", "h", size=1)
    # Force the row to be ancient via direct SQL — quicker than freezegun.
    old_ts = _time.time() - (365 * 86400)
    store._conn.execute(
        "UPDATE cloud_hash_cache SET computed_ts = ? WHERE cloud_file_id = 'a'",
        (old_ts,),
    )
    store._conn.commit()
    removed = store.purge_stale_cloud_hashes(max_age_days=90.0)
    assert removed == 1
    assert store.get_cloud_hash("gdrive:x", "a", "e") is None
    store.close()
