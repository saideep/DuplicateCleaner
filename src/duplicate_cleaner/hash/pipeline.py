"""Multi-stage hashing — size bucket → partial BLAKE3 → full BLAKE3, cache-backed."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.store import Store

log = logging.getLogger(__name__)

PARTIAL_CHUNK = 64 * 1024  # 64 KB head + tail sampled for the partial hash.
FULL_READ_BLOCK = 1 << 20  # 1 MiB chunks for full-hash reads.

# Flush the scan-stage batch every N inserts so we don't hold one giant
# transaction open on huge trees.
_STAGE_FLUSH_INTERVAL = 5000


@dataclass(frozen=True)
class HashedRecord:
    """A FileRecord plus its full BLAKE3 hash."""

    path: Path
    size: int
    mtime: float
    inode: int
    dev: int
    nlink: int
    full_hash: str


def _hash_partial(path: Path, size: int) -> str:
    h = blake3.blake3()
    with path.open("rb") as f:
        head_len = min(PARTIAL_CHUNK, size)
        h.update(f.read(head_len))
        if size > PARTIAL_CHUNK * 2:
            f.seek(size - PARTIAL_CHUNK)
            h.update(f.read(PARTIAL_CHUNK))
    return str(h.hexdigest())


def _hash_full(path: Path) -> str:
    h = blake3.blake3()
    with path.open("rb") as f:
        while True:
            chunk = f.read(FULL_READ_BLOCK)
            if not chunk:
                break
            h.update(chunk)
    return str(h.hexdigest())


def hash_records(
    records: Iterable[FileRecord],
    store: Store,
) -> Iterator[HashedRecord]:
    """Two-pass streaming: stage every walker row into SQLite, then hash by size bucket.

    Pass 1 writes ``(path, size, mtime, inode, dev, nlink)`` into the
    ``scan_stage`` table so we never hold the entire walker output in
    memory. Pass 2 asks SQLite for every size-bucket with more than one
    member and hashes only those. Memory stays O(size-buckets), not
    O(files).

    G3: each call generates a fresh ``scan_id`` and every read/write is
    scoped to it. Two concurrent ``dc scan`` invocations sharing this
    cache DB no longer wipe each other's staged rows. Rows older than
    ``_STAGE_TTL_SECONDS`` are swept at the start to reap crashed prior scans.
    """
    scan_id = store.new_scan_id()
    store.sweep_stale_scan_stage()
    staged = 0
    for rec in records:
        store.stage_record(
            rec.path,
            scan_id=scan_id,
            size=rec.size,
            mtime=rec.mtime,
            inode=rec.inode,
            dev=rec.dev,
            nlink=rec.nlink,
        )
        staged += 1
        if staged % _STAGE_FLUSH_INTERVAL == 0:
            store.commit()
    store.commit()

    try:
        for bucket in store.iter_duplicate_size_buckets(scan_id):
            group = [
                FileRecord(
                    path=Path(path),
                    size=size,
                    mtime=mtime,
                    inode=inode,
                    dev=dev,
                    nlink=nlink,
                )
                for path, size, mtime, inode, dev, nlink in bucket
            ]
            yield from _hash_size_bucket(group, store)
    finally:
        # Always drop this scan's staged rows — even on generator abort.
        store.clear_scan_stage(scan_id)


def _hash_size_bucket(
    group: list[FileRecord], store: Store
) -> Iterator[HashedRecord]:
    # For each record: fetch (partial, full) from cache; compute partial if missing.
    with_partial: list[tuple[FileRecord, str, str | None]] = []
    for rec in group:
        cached_partial, cached_full = store.get_cached_hash(
            rec.path, rec.size, rec.mtime
        )
        partial = cached_partial
        if partial is None:
            try:
                partial = _hash_partial(rec.path, rec.size)
            except OSError as e:
                log.warning("Partial hash failed for %s: %s", rec.path, e)
                continue
            store.upsert_file(
                rec.path,
                size=rec.size,
                mtime=rec.mtime,
                inode=rec.inode,
                dev=rec.dev,
                partial_hash=partial,
                full_hash=cached_full,
            )
        with_partial.append((rec, partial, cached_full))

    # Bucket by partial hash. Buckets with <2 members cannot be duplicates.
    by_partial: dict[str, list[tuple[FileRecord, str | None]]] = defaultdict(list)
    for rec, partial, cached_full in with_partial:
        by_partial[partial].append((rec, cached_full))

    for sub in by_partial.values():
        if len(sub) < 2:
            continue
        for rec, cached_full in sub:
            full = cached_full
            if full is None:
                try:
                    full = _hash_full(rec.path)
                except OSError as e:
                    log.warning("Full hash failed for %s: %s", rec.path, e)
                    continue
                store.upsert_file(
                    rec.path,
                    size=rec.size,
                    mtime=rec.mtime,
                    inode=rec.inode,
                    dev=rec.dev,
                    partial_hash=None,
                    full_hash=full,
                )
            yield HashedRecord(
                path=rec.path,
                size=rec.size,
                mtime=rec.mtime,
                inode=rec.inode,
                dev=rec.dev,
                nlink=rec.nlink,
                full_hash=full,
            )
