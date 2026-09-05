"""Exact-duplicate grouping — bucket hashed records by full BLAKE3."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from duplicate_cleaner.hash.pipeline import HashedRecord


@dataclass(frozen=True)
class Group:
    """A set of files sharing the same full-content hash."""

    hash: str
    size: int
    members: list[HashedRecord] = field(default_factory=list)


def group_by_hash(
    records: Iterable[HashedRecord],
    min_size_bytes: int = 0,
) -> Iterator[Group]:
    """Yield exact-duplicate groups (≥2 members) filtered by min_size_bytes."""
    buckets: dict[str, list[HashedRecord]] = defaultdict(list)
    for rec in records:
        if rec.size < min_size_bytes:
            continue
        buckets[rec.full_hash].append(rec)

    for h, members in buckets.items():
        seen: set[str] = set()
        unique: list[HashedRecord] = []
        for m in members:
            key = str(m.path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        if len(unique) < 2:
            continue
        yield Group(hash=h, size=unique[0].size, members=unique)
