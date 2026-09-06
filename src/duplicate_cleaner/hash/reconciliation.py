"""Cloud + local hash reconciliation — bring every size-bucket into a single canonical BLAKE3 space.

The exact-dedup pipeline compares BLAKE3 hashes across all members.  Cloud
sources hand us a foreign hash (MD5 for Drive, SHA-256 for OneDrive) with
no way to re-align without downloading.  This module drives that
re-alignment as cheaply as possible: it only issues a stream-download for a
cloud member whose peers use a different algorithm AND whose BLAKE3 isn't
already cached in :class:`~duplicate_cleaner.store.Store`.

v0.2.1 wires :func:`reconcile_cross_source` into ``cli.py::scan`` after the
size-bucket → partial-hash → full-hash pipeline.  Same-algo buckets stay
zero-cost; cross-algo buckets stream cloud bytes through the source's
``read_bytes`` iterator (memory-flat) and cache the resulting BLAKE3 so
subsequent scans re-use it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.store import Store

if TYPE_CHECKING:
    from duplicate_cleaner.hash.pipeline import HashedRecord
    from duplicate_cleaner.report.schema import NotYetHashedBucket
    from duplicate_cleaner.sources.base import Source

log = logging.getLogger(__name__)

_DEFAULT_MAX_CLOUD_DOWNLOAD_MB = 1000.0
_MAX_CACHE_AGE_DAYS = 90.0


@dataclass(frozen=True)
class ReconciledRecord:
    """A FileRecord whose ``blake3`` is now known — the canonical algo for grouping."""

    record: FileRecord
    blake3: str
    from_cache: bool = False


@dataclass
class ReconciliationBudget:
    """Track cumulative cloud bytes downloaded across all buckets in one scan."""

    max_download_bytes: int
    downloaded_bytes: int = 0
    deferred_buckets: list[tuple[int, list[str]]] = field(default_factory=list)

    def would_exceed(self, additional: int) -> bool:
        """Return True if downloading ``additional`` bytes would blow the cap."""
        return self.downloaded_bytes + additional > self.max_download_bytes

    def record_download(self, n_bytes: int) -> None:
        """Register ``n_bytes`` against the cap."""
        self.downloaded_bytes += n_bytes

    def defer_bucket(self, size: int, cloud_ids: list[str]) -> None:
        """Record that a bucket was left un-hashed because of the cap."""
        self.deferred_buckets.append((size, list(cloud_ids)))


def make_budget(max_download_mb: float | None) -> ReconciliationBudget:
    """Return a budget object from a CLI ``--max-cloud-download-mb`` value."""
    mb = _DEFAULT_MAX_CLOUD_DOWNLOAD_MB if max_download_mb is None else float(max_download_mb)
    if mb < 0:
        mb = 0.0
    return ReconciliationBudget(max_download_bytes=int(mb * 1024 * 1024))


def _algo_prefix(hash_value: str | None) -> str | None:
    """Return ``"md5"`` from ``"md5:abcd..."``; ``None`` for unset/unprefixed."""
    if not hash_value or ":" not in hash_value:
        return None
    return hash_value.split(":", 1)[0]


def _members_share_algo(members: list[FileRecord]) -> str | None:
    """Return the shared algorithm prefix if every member carries the same one."""
    prefixes: set[str] = set()
    for m in members:
        p = _algo_prefix(m.foreign_hash)
        if p is None:
            return None
        prefixes.add(p)
    if len(prefixes) == 1:
        return next(iter(prefixes))
    return None


def _shared_algo(members: list[FileRecord]) -> str | None:
    """Alias — the public spelling used by callers wanting the shared prefix."""
    return _members_share_algo(members)


def reconcile_bucket(
    members: list[FileRecord],
    store: Store,
    read_bytes_fn: Callable[[FileRecord], Iterator[bytes]],
    *,
    budget: ReconciliationBudget | None = None,
    local_blake3_lookup: Callable[[FileRecord], str | None] | None = None,
) -> list[ReconciledRecord] | None:
    """Reconcile a single size-bucket to a canonical BLAKE3 per member.

    Returns ``None`` when the bucket cannot be reconciled without exceeding
    the download budget — the caller marks it "not-yet-hashed" in the
    report.  Otherwise returns one :class:`ReconciledRecord` per input
    member with a populated ``blake3`` field.
    """
    if len(members) < 2:
        # Singleton across sources — never a duplicate candidate; skip.
        return []

    shared = _shared_algo(members)
    if shared is not None:
        # B5: any shared algorithm — not just blake3 — means we can group by
        # foreign_hash directly and skip the download.  Two files that share
        # md5 (e.g. two Google accounts) still bucket into the same group;
        # they simply carry the foreign digest as their canonical ``blake3``
        # field.  Group-membership is the only downstream consumer, so the
        # tag string is opaque as long as it collides consistently.
        return [
            ReconciledRecord(
                record=m,
                blake3=(m.foreign_hash or "").split(":", 1)[1],
                from_cache=True,
            )
            for m in members
        ]

    reconciled: list[ReconciledRecord] = []
    to_download: list[FileRecord] = []
    for m in members:
        if m.source_id == "local":
            local_hash = None
            if local_blake3_lookup is not None:
                local_hash = local_blake3_lookup(m)
            if local_hash is None:
                # Callers typically pre-hash local; if not, mark for a full
                # local read (still cheap — no network).
                to_download.append(m)
            else:
                reconciled.append(
                    ReconciledRecord(record=m, blake3=local_hash, from_cache=True)
                )
            continue
        if not m.cloud_file_id or not m.etag:
            log.debug("Cloud member %s missing cloud_file_id/etag; skipping", m.path)
            continue
        cached = store.get_cloud_hash(m.source_id, m.cloud_file_id, m.etag)
        if cached is not None:
            reconciled.append(
                ReconciledRecord(record=m, blake3=cached, from_cache=True)
            )
        else:
            to_download.append(m)

    if budget is not None and to_download:
        cloud_download_bytes = sum(
            m.size for m in to_download if m.source_id != "local"
        )
        if budget.would_exceed(cloud_download_bytes):
            budget.defer_bucket(
                members[0].size,
                [m.cloud_file_id or "" for m in to_download if m.source_id != "local"],
            )
            return None

    for m in to_download:
        try:
            h = _stream_blake3(read_bytes_fn(m))
        except OSError as exc:
            log.warning("Reconcile stream failed for %s: %s", m.path, exc)
            continue
        if m.source_id != "local" and m.cloud_file_id and m.etag:
            store.put_cloud_hash(m.source_id, m.cloud_file_id, m.etag, h, m.size)
            if budget is not None:
                budget.record_download(m.size)
        reconciled.append(ReconciledRecord(record=m, blake3=h, from_cache=False))

    return reconciled


def _stream_blake3(chunks: Iterator[bytes]) -> str:
    """Consume ``chunks`` into a BLAKE3 digest hex string."""
    h = blake3.blake3()
    for chunk in chunks:
        h.update(chunk)
    return str(h.hexdigest())


def purge_stale_cache(store: Store, max_age_days: float = _MAX_CACHE_AGE_DAYS) -> int:
    """Drop cloud_hash_cache rows older than ``max_age_days``."""
    return store.purge_stale_cloud_hashes(max_age_days=max_age_days)


def _record_from_hashed(h: HashedRecord) -> FileRecord:
    """Rebuild a FileRecord surface from a HashedRecord so reconcile_bucket can read it."""
    return FileRecord(
        path=h.path,
        size=h.size,
        mtime=h.mtime,
        inode=h.inode,
        dev=h.dev,
        nlink=h.nlink,
        is_archive_member=h.is_archive_member,
        is_bundle=h.is_bundle,
        source_id=h.source_id,
        foreign_hash=h.foreign_hash,
        etag=h.etag,
        cloud_file_id=h.cloud_file_id,
        owner=h.owner,
        is_shared=h.is_shared,
    )


def reconcile_cross_source(
    records: list[HashedRecord],
    sources: dict[str, Source],
    store: Store,
    budget: ReconciliationBudget,
) -> tuple[list[HashedRecord], list[NotYetHashedBucket]]:
    """Align cross-source size buckets to a single canonical hash space.

    Iterates the flat ``records`` list by ``size``.  For each size bucket
    with ≥ 2 members whose ``source_id`` values differ (i.e. a genuine
    cross-source candidate), invokes :func:`reconcile_bucket` and rewrites
    every touched record's ``full_hash`` to the canonical value.  Members
    are also flagged ``reconciled=True`` so the report can surface the
    provenance.  Same-source buckets are left untouched (the pipeline
    already handed us a matching BLAKE3 for local peers, and same-algo
    cloud peers already share a ``foreign_hash`` string).

    Buckets that would exceed the download cap are recorded in the
    returned list of :class:`NotYetHashedBucket` and their members are
    left with their original per-source hashes — surfaced but not grouped.

    Archive members are excluded from reconciliation — their hashes are
    already canonical BLAKE3 emitted by the archive walker, and their
    "path" is a virtual ``outer::inner`` string with no ``source_id`` for
    the cloud dispatch to consult.
    """
    from duplicate_cleaner.report.schema import NotYetHashedBucket

    if not records:
        return records, []

    def _read_bytes(rec: FileRecord) -> Iterator[bytes]:
        src = sources.get(rec.source_id)
        if src is None:
            raise OSError(
                f"no source registered for id {rec.source_id!r} — cannot "
                "reconcile"
            )
        return src.read_bytes(rec)

    # Index records that participate in reconciliation by size.  Excluded:
    # archive members (virtual paths, no source dispatch) and any record
    # already flagged reconciled (defensive — should never happen on the
    # single-pass path but keeps the function idempotent).
    by_size: dict[int, list[int]] = defaultdict(list)
    for idx, h in enumerate(records):
        if h.is_archive_member:
            continue
        by_size[h.size].append(idx)

    # Build a local-BLAKE3 lookup keyed by (path, size).  Local records
    # emerge from the pipeline with a real BLAKE3 in ``full_hash``; the
    # reconciler consults this instead of re-hashing local bytes.
    local_hashes: dict[tuple[str, int], str] = {
        (str(records[idx].path), records[idx].size): records[idx].full_hash
        for indices in by_size.values()
        for idx in indices
        if records[idx].source_id == "local"
    }

    def _local_lookup(m: FileRecord) -> str | None:
        return local_hashes.get((str(m.path), m.size))

    updated: dict[int, HashedRecord] = {}
    not_yet: list[NotYetHashedBucket] = []

    for size, indices in by_size.items():
        if len(indices) < 2:
            continue
        source_ids = {records[i].source_id for i in indices}
        if len(source_ids) < 2:
            # Same-source bucket — the pipeline (local) or the foreign_hash
            # stamp (cloud) has already given every member a hash that
            # groups correctly.  Nothing to reconcile.
            continue
        members = [_record_from_hashed(records[i]) for i in indices]
        result = reconcile_bucket(
            members,
            store,
            _read_bytes,
            budget=budget,
            local_blake3_lookup=_local_lookup,
        )
        if result is None:
            # Budget-exceeded — surface the whole bucket as not-yet-hashed
            # and leave every member's existing hash in place.
            not_yet.append(
                NotYetHashedBucket(
                    paths=[str(records[i].path) for i in indices],
                    size=size,
                    reason="budget_exceeded",
                )
            )
            continue
        # Reconciled → rewrite each touched member's full_hash to the
        # canonical value from ``reconcile_bucket`` and flag it.
        by_path: dict[str, str] = {
            str(r.record.path): r.blake3 for r in result
        }
        for i in indices:
            rec = records[i]
            new_hash = by_path.get(str(rec.path))
            if new_hash is None:
                # ``reconcile_bucket`` dropped the record (e.g. cloud member
                # missing cloud_file_id/etag) — leave the original hash in
                # place so it can still surface as a singleton.
                continue
            if new_hash == rec.full_hash and not _needs_reconciled_flag(rec):
                continue
            updated[i] = _replace_hash(rec, new_hash)

    if not updated:
        return records, not_yet

    out: list[HashedRecord] = [
        updated.get(idx, rec) for idx, rec in enumerate(records)
    ]
    return out, not_yet


def _needs_reconciled_flag(rec: HashedRecord) -> bool:
    """Cloud members always get flagged when the reconcile pass touches their bucket."""
    return rec.source_id != "local"


def _replace_hash(rec: HashedRecord, new_hash: str) -> HashedRecord:
    """Return a copy of ``rec`` with ``full_hash`` replaced and ``reconciled=True``."""
    from duplicate_cleaner.hash.pipeline import HashedRecord

    return HashedRecord(
        path=rec.path,
        size=rec.size,
        mtime=rec.mtime,
        inode=rec.inode,
        dev=rec.dev,
        nlink=rec.nlink,
        full_hash=new_hash,
        is_archive_member=rec.is_archive_member,
        is_bundle=rec.is_bundle,
        source_id=rec.source_id,
        is_shared=rec.is_shared,
        foreign_hash=rec.foreign_hash,
        etag=rec.etag,
        cloud_file_id=rec.cloud_file_id,
        owner=rec.owner,
        reconciled=True,
    )
