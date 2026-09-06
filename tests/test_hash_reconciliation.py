"""Hash reconciliation — cache hits, download budget, singleton skip, cross-algo download."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.hash.reconciliation import (
    ReconciliationBudget,
    make_budget,
    reconcile_bucket,
    reconcile_cross_source,
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
    """B5: two members with different algos force the cache-lookup path.

    The same-algo shortcut (B5) fires first when EVERY member shares an algo.
    To exercise the cache-hit branch we mix algos — one gdrive/md5, one
    onedrive/sha256 — so ``_shared_algo`` returns None and the reconcile
    stage falls through to the per-member cache lookup.
    """
    store = Store(path=tmp_path / "cache.db")
    store.put_cloud_hash("gdrive:x", "a", "etagA", "cached-hash", size=1000)
    store.put_cloud_hash("onedrive:y", "b", "etagB", "cached-hash", size=1000)

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
            source="onedrive:y",
            size=1000,
            foreign="sha256:BBB",
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


def test_shared_md5_algo_skips_download(tmp_path: Path) -> None:
    """B5: two members sharing md5 — not just blake3 — skip download entirely."""
    store = Store(path=tmp_path / "cache.db")
    calls: list[FileRecord] = []

    def _reader(r: FileRecord) -> Iterator[bytes]:
        calls.append(r)
        yield b""

    members = [
        _rec(
            source="gdrive:a",
            size=10,
            foreign="md5:DEADBEEF",
            cloud_id="a1",
            etag="e1",
        ),
        _rec(
            source="gdrive:b",
            size=10,
            foreign="md5:DEADBEEF",
            cloud_id="b1",
            etag="e2",
        ),
    ]
    got = reconcile_bucket(members, store, _reader)
    assert got is not None
    assert calls == []  # B5: no download for same-algo bucket
    # Both files carry the same MD5 → they group under the same key.
    assert {r.blake3 for r in got} == {"DEADBEEF"}
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


class _StubSource:
    """Minimal ``Source`` shim — hands back a canned byte payload per record."""

    def __init__(self, sid: str, payloads: dict[str, bytes]) -> None:
        self.id = sid
        self.is_read_only_scan = True
        self._payloads = payloads
        self.reads: list[str] = []

    def read_bytes(
        self, record: FileRecord, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        key = record.cloud_file_id or str(record.path)
        self.reads.append(key)
        payload = self._payloads.get(key)
        if payload is None:
            raise OSError(f"no payload registered for {key!r}")
        yield payload


def _hashed_local(
    path: Path,
    *,
    size: int,
    full_hash: str,
    mtime: float = 0.0,
) -> HashedRecord:
    return HashedRecord(
        path=path,
        size=size,
        mtime=mtime,
        inode=1,
        dev=1,
        nlink=1,
        full_hash=full_hash,
        source_id="local",
    )


def _hashed_cloud(
    *,
    source: str,
    size: int,
    foreign: str,
    cloud_id: str,
    etag: str,
    path: str | None = None,
) -> HashedRecord:
    return HashedRecord(
        path=Path(path or f"{source}://virtual/{cloud_id}"),
        size=size,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        full_hash=foreign,
        source_id=source,
        foreign_hash=foreign,
        etag=etag,
        cloud_file_id=cloud_id,
    )


def test_cross_algo_bucket_reconciles_and_groups(tmp_path: Path) -> None:
    """A local BLAKE3 record + a cloud MD5 record with matching bytes must
    end up with the same canonical hash after ``reconcile_cross_source``,
    so ``group_by_hash`` treats them as duplicates."""
    from duplicate_cleaner.compare.exact import group_by_hash

    payload = b"cross-algo-match-payload"
    local_hash = _blake3(payload)
    local_path = tmp_path / "keep.bin"
    local_path.write_bytes(payload)

    records = [
        _hashed_local(local_path, size=len(payload), full_hash=local_hash),
        _hashed_cloud(
            source="gdrive:x",
            size=len(payload),
            foreign="md5:aabbcc",
            cloud_id="cid1",
            etag="etag1",
        ),
    ]
    store = Store(path=tmp_path / "cache.db")
    src = _StubSource("gdrive:x", {"cid1": payload})
    budget = make_budget(1000.0)
    out, not_yet = reconcile_cross_source(
        records, {"local": _StubSource("local", {}), "gdrive:x": src}, store, budget
    )
    assert not_yet == []
    assert {r.full_hash for r in out} == {local_hash}
    groups = list(group_by_hash(iter(out)))
    assert len(groups) == 1
    assert {m.source_id for m in groups[0].members} == {"local", "gdrive:x"}
    store.close()


def test_reconciled_group_marked_in_report(tmp_path: Path) -> None:
    """The cloud member reconciled from cross-algo bytes carries ``reconciled=True``."""
    payload = b"payload-for-report-flag"
    local_hash = _blake3(payload)
    local_path = tmp_path / "keep.bin"
    local_path.write_bytes(payload)

    records = [
        _hashed_local(local_path, size=len(payload), full_hash=local_hash),
        _hashed_cloud(
            source="gdrive:x",
            size=len(payload),
            foreign="md5:zz",
            cloud_id="cid2",
            etag="etag2",
        ),
    ]
    store = Store(path=tmp_path / "cache.db")
    src = _StubSource("gdrive:x", {"cid2": payload})
    out, _ = reconcile_cross_source(
        records,
        {"local": _StubSource("local", {}), "gdrive:x": src},
        store,
        make_budget(1000.0),
    )
    by_source = {r.source_id: r for r in out}
    assert by_source["gdrive:x"].reconciled is True
    # Local member's hash was already canonical BLAKE3 — the flag is
    # noise on locals so it stays False.
    assert by_source["local"].reconciled is False
    store.close()


def test_budget_exceeded_bucket_skipped(tmp_path: Path) -> None:
    """A bucket over the ``--max-cloud-download-mb`` cap lands in
    ``not_yet_hashed_buckets`` and no download runs."""
    payload = b"payload"
    local_hash = _blake3(payload)
    huge_size = 100 * 1024 * 1024
    local_path = tmp_path / "big.bin"
    local_path.write_bytes(payload)  # size on disk doesn't matter — records carry it

    records = [
        HashedRecord(
            path=local_path,
            size=huge_size,
            mtime=0.0,
            inode=1,
            dev=1,
            nlink=1,
            full_hash=local_hash,
            source_id="local",
        ),
        _hashed_cloud(
            source="gdrive:x",
            size=huge_size,
            foreign="md5:zz",
            cloud_id="huge",
            etag="e",
        ),
    ]
    store = Store(path=tmp_path / "cache.db")
    src = _StubSource("gdrive:x", {"huge": payload})
    # 1 KiB cap — cannot fit a 100 MB download.
    budget = ReconciliationBudget(max_download_bytes=1024)
    out, not_yet = reconcile_cross_source(
        records,
        {"local": _StubSource("local", {}), "gdrive:x": src},
        store,
        budget,
    )
    assert src.reads == []
    assert len(not_yet) == 1
    assert not_yet[0].size == huge_size
    assert not_yet[0].reason == "budget_exceeded"
    assert str(local_path) in not_yet[0].paths
    # Records untouched — original per-source hashes kept.
    assert {r.full_hash for r in out} == {local_hash, "md5:zz"}
    store.close()


def test_singleton_size_bucket_skips_reconciliation(tmp_path: Path) -> None:
    """A size bucket with a single member never triggers a download."""
    payload = b"single"
    local_hash = _blake3(payload)
    local_path = tmp_path / "only.bin"
    local_path.write_bytes(payload)
    records = [
        _hashed_local(local_path, size=len(payload), full_hash=local_hash),
    ]
    store = Store(path=tmp_path / "cache.db")
    src = _StubSource("gdrive:x", {})
    out, not_yet = reconcile_cross_source(
        records, {"local": _StubSource("local", {}), "gdrive:x": src}, store, make_budget(1000.0)
    )
    assert src.reads == []
    assert not_yet == []
    assert out == records
    store.close()


def test_cache_hit_avoids_download(tmp_path: Path) -> None:
    """On the second scan the ``cloud_hash_cache`` entry short-circuits the download."""
    payload = b"second-scan-cache-hit"
    local_hash = _blake3(payload)
    local_path = tmp_path / "keep.bin"
    local_path.write_bytes(payload)

    def _make_records() -> list[HashedRecord]:
        return [
            _hashed_local(local_path, size=len(payload), full_hash=local_hash),
            _hashed_cloud(
                source="gdrive:x",
                size=len(payload),
                foreign="md5:zz",
                cloud_id="cid-cache",
                etag="etag-stable",
            ),
        ]

    store = Store(path=tmp_path / "cache.db")
    src = _StubSource("gdrive:x", {"cid-cache": payload})
    # First run — downloads and caches.
    reconcile_cross_source(
        _make_records(),
        {"local": _StubSource("local", {}), "gdrive:x": src},
        store,
        make_budget(1000.0),
    )
    assert src.reads == ["cid-cache"]
    # Second run with a fresh source — cache hit means no download.
    src2 = _StubSource("gdrive:x", {"cid-cache": payload})
    reconcile_cross_source(
        _make_records(),
        {"local": _StubSource("local", {}), "gdrive:x": src2},
        store,
        make_budget(1000.0),
    )
    assert src2.reads == []
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
