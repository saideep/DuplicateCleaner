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
    """A FileRecord plus its full BLAKE3 hash.

    v0.2 sub-milestone 5e — carries the cloud-side fields the scorer
    consults for cross-source signals.  ``source_id`` defaults to
    ``"local"`` so every v0.1.1 construction site keeps working; cloud
    sources stamp their own id.  ``is_shared`` marks shared-with-me cloud
    files (informational-only invariant, enforced in the scorer).

    v0.2.1 — cloud metadata fields (``foreign_hash``, ``etag``,
    ``cloud_file_id``, ``owner``) are preserved from the source
    ``FileRecord`` so the post-hash reconciliation pass and the mover
    dispatch on cross-source groups can find them.  ``reconciled`` marks
    members whose ``full_hash`` was normalised by cross-algo reconciliation.
    """

    path: Path
    size: int
    mtime: float
    inode: int
    dev: int
    nlink: int
    full_hash: str
    is_archive_member: bool = False
    is_bundle: bool = False
    # v0.2 additions — all defaulted so v0.1.1 constructions keep working.
    source_id: str = "local"
    is_shared: bool = False
    is_singleton_across_sources: bool = False
    # v0.2.1 additions — preserve cloud-side fields so reconciliation and
    # the mover can dispatch cross-source groups.
    foreign_hash: str | None = None
    etag: str | None = None
    cloud_file_id: str | None = None
    owner: str | None = None
    reconciled: bool = False


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
    *,
    include_singletons: bool = False,
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
    # Records that arrive with a hash already computed (archive members,
    # bundle roll-ups) are yielded directly and never enter the staging
    # table — they cannot benefit from size-bucket pruning because their
    # ``path`` may be virtual (contains ``::``) and they don't map to a
    # single on-disk file the pipeline could re-hash.
    prehashed: list[HashedRecord] = []
    staged = 0
    for rec in records:
        if rec.precomputed_full_hash is not None:
            prehashed.append(
                HashedRecord(
                    path=rec.path,
                    size=rec.size,
                    mtime=rec.mtime,
                    inode=rec.inode,
                    dev=rec.dev,
                    nlink=rec.nlink,
                    full_hash=rec.precomputed_full_hash,
                    is_archive_member=rec.is_archive_member,
                    is_bundle=rec.is_bundle,
                    source_id=rec.source_id,
                    is_shared=rec.is_shared,
                    foreign_hash=rec.foreign_hash,
                    etag=rec.etag,
                    cloud_file_id=rec.cloud_file_id,
                    owner=rec.owner,
                )
            )
            continue
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
    yield from prehashed

    try:
        if include_singletons:
            # Yield synthetic HashedRecords for size-singleton files. They
            # never enter the partial/full-hash stages; the marker prefix
            # keeps them out of legitimate hash groups downstream, while
            # still surfacing them for the singleton section of the report.
            for row in store.iter_singleton_stage_records(scan_id):
                path_str, size, mtime, inode, dev, nlink = row
                yield HashedRecord(
                    path=Path(path_str),
                    size=size,
                    mtime=mtime,
                    inode=inode,
                    dev=dev,
                    nlink=nlink,
                    full_hash=f"singleton-by-size:{size}:{path_str}",
                )
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
            yield from _hash_size_bucket(
                group, store, include_singletons=include_singletons
            )
    finally:
        # Always drop this scan's staged rows — even on generator abort.
        store.clear_scan_stage(scan_id)


def _hash_size_bucket(
    group: list[FileRecord],
    store: Store,
    *,
    include_singletons: bool = False,
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

    for partial, sub in by_partial.items():
        if len(sub) < 2:
            # H7: a size-collided file with a unique partial hash cannot be
            # a duplicate, but it MUST still surface as a singleton — the
            # old code silently dropped these because
            # ``iter_singleton_stage_records`` only yielded files whose
            # SIZE bucket had exactly one member. Emit a synthetic
            # HashedRecord marked with a "singleton-by-partial" fake hash
            # so the downstream cli singleton loop picks it up.
            if include_singletons:
                for rec, _cached_full in sub:
                    yield HashedRecord(
                        path=rec.path,
                        size=rec.size,
                        mtime=rec.mtime,
                        inode=rec.inode,
                        dev=rec.dev,
                        nlink=rec.nlink,
                        full_hash=(
                            f"singleton-by-partial:{partial}:{rec.path}"
                        ),
                    )
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
