"""Cloud + local hash reconciliation — bring every size-bucket into a single canonical BLAKE3 space.

The exact-dedup pipeline compares BLAKE3 hashes across all members.  Cloud
sources hand us a foreign hash (MD5 for Drive, SHA-256 for OneDrive) with
no way to re-align without downloading.  This module drives that
re-alignment as cheaply as possible: it only issues a stream-download for a
cloud member whose peers use a different algorithm AND whose BLAKE3 isn't
already cached in :class:`~duplicate_cleaner.store.Store`.

Sub-phase 2 exposes the module and its data types; sub-phase 5 wires it
into ``hash/pipeline.py``.  A partial hookup here keeps behaviour of the
existing 158-test suite unchanged — the reconcile step is invoked only when
a size bucket contains ``source_id != 'local'`` members.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.store import Store

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
