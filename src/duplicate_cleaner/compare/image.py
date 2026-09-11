"""Image near-duplicate detection — perceptual hash + Hamming clustering.

v0.7 milestone.  Byte-identical images are already collapsed by
:mod:`compare.exact` (BLAKE3 grouping).  This module catches the harder
case: two files that render as the same photo but differ byte-for-byte
because of a re-encode, resize, colour-space tweak, or metadata rewrite.

Detection pipeline:

1.  Filter :class:`HashedRecord` inputs down to extensions in
    :data:`IMAGE_EXTENSIONS`.
2.  Compute a 256-bit perceptual hash (``imagehash.phash`` at
    ``hash_size=16``) for every image, backed by
    :class:`~duplicate_cleaner.store.Store` so repeat scans reuse the
    result while file stats stay put.  256-bit was picked over the
    library default 64-bit to keep false positives low on near-white
    photos and simple graphics where the 8x8 DCT collapses too many
    distinct pictures into one hash.
3.  Union-find over the pairwise Hamming graph collapses N-way near-dup
    clusters into ONE :class:`ImageNearDupGroup` instead of the
    ``N*(N-1)/2`` pair groups a naive emitter would produce.  This
    mirrors the v0.4 tree aggregator's union-find fix (see AUDIT_LOG
    entry K2 for v0.4).
4.  Groups whose members ALL share the same full BLAKE3 hash are
    discarded — they are already surfaced by :mod:`compare.exact` and
    double-counting them here would inflate the report's reclaim number.
5.  Groups whose smallest member is under ``min_size`` bytes are
    discarded — thumbnails, favicons, and 100-byte "images" produce
    perceptually-degenerate hashes and cluster far too aggressively.

Discards in an image-near-dup group flow through the standard per-file
``send2trash`` rail; no directory-scoped semantics.  The mover treats an
``image-near-dup`` group identically to an ``exact`` group at the
send2trash level — the only difference is how the group was assembled.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.store import Store

log = logging.getLogger(__name__)

# pHash bit-length when using ``imagehash.phash(hash_size=16)``.  Locked
# in so the Hamming threshold semantics stay stable across releases —
# ``distance_threshold=8`` means "≤8 differing bits out of 256".
_PHASH_HASH_SIZE = 16
_PHASH_BITS = _PHASH_HASH_SIZE * _PHASH_HASH_SIZE  # 256

# Case-insensitive extension gate.  Anything not in this set is treated
# as "not an image" and skipped without touching Pillow (heavy import,
# slow open on non-image bytes).
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".heif",
        ".webp",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
    }
)


def is_image_path(path: Path) -> bool:
    """Return True when ``path`` has an extension in :data:`IMAGE_EXTENSIONS`."""
    return path.suffix.lower() in IMAGE_EXTENSIONS


@dataclass(frozen=True)
class ImageNearDupGroup:
    """One connected component of perceptually-similar images.

    ``max_distance`` is the worst pairwise Hamming distance inside the
    component — a downstream reader can compute
    ``similarity = 1 - max_distance / 256`` to gauge cluster tightness.
    ``size_range`` records the smallest and largest member size in bytes
    so the HTML report can flag e.g. a 60 KB thumbnail clustered with a
    3 MB original.
    """

    members: list[HashedRecord]
    max_distance: int
    size_range: tuple[int, int]
    phash_bits: int = _PHASH_BITS
    # Positional per-member pHash hex string.  Kept alongside ``members``
    # so the HTML renderer can display the pair without recomputing.
    phashes: list[str] = field(default_factory=list)


def compute_phash(path: Path) -> str | None:
    """Return a hex-encoded 256-bit perceptual hash for ``path`` or None.

    Uses ``imagehash.phash`` with ``hash_size=16``.  Any failure to open
    the file as an image (unknown format, corrupt bytes, Pillow decode
    error, OS permission error) yields ``None`` — the caller drops the
    record from near-dup grouping.  imagehash and Pillow are imported
    lazily inside the function so the module import stays cheap on a
    scan that touches zero images.
    """
    try:
        import imagehash  # type: ignore[import-untyped]
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - image extras missing in dev
        log.debug("imagehash / Pillow unavailable; skipping pHash on %s", path)
        return None
    try:
        with Image.open(path) as im:
            phash = imagehash.phash(im, hash_size=_PHASH_HASH_SIZE)
    except (OSError, ValueError, UnidentifiedImageError) as e:
        log.debug("pHash failed on %s: %s", path, e)
        return None
    return str(phash)


def hamming_distance(a: str, b: str) -> int:
    """Return the bit distance between two hex-encoded perceptual hashes.

    Refuses (via ``ValueError``) on empty strings or length mismatch —
    mixing a 64-bit and a 256-bit pHash would give a nonsensical result.
    Callers should filter Nones before invoking this.
    """
    if not a or not b:
        raise ValueError("hamming_distance requires two non-empty hex strings")
    if len(a) != len(b):
        raise ValueError(
            f"pHash length mismatch: len(a)={len(a)}, len(b)={len(b)}"
        )
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _load_or_compute_phash(
    rec: HashedRecord,
    store: Store | None,
) -> str | None:
    """Return ``rec``'s pHash — from cache when stats match, else recomputed.

    A miss populates the cache.  A recomputation failure returns None
    without caching; the caller drops the record from grouping.  The
    store parameter is optional so unit tests that want raw grouping
    behavior can skip cache plumbing entirely.
    """
    if store is not None:
        cached = store.get_cached_phash(rec.path, rec.size, rec.mtime)
        if cached is not None:
            return cached
    phash = compute_phash(rec.path)
    if phash is None:
        return None
    if store is not None:
        store.put_phash(rec.path, rec.size, rec.mtime, phash)
    return phash


def find_image_near_duplicates(
    records: Iterable[HashedRecord],
    *,
    distance_threshold: int = 8,
    min_size: int = 10_000,
    store: Store | None = None,
) -> list[ImageNearDupGroup]:
    """Return one :class:`ImageNearDupGroup` per near-duplicate cluster.

    ``distance_threshold`` is the maximum Hamming distance (in bits) at
    which two 256-bit pHashes are considered near-duplicates.  Default 8
    ≈ 3% of the hash budget — matches the imagehash library convention
    for "same image, minor difference".

    ``min_size`` filters out records under N bytes at intake (thumbnails
    / favicons whose pHash collapses too aggressively) AND filters out
    any resulting group whose smallest member is under the same bound.
    Both rails run so a mixed cluster (one 3 MB original + one 200-byte
    thumbnail) is dropped entirely rather than emitting a spurious
    "duplicate" claim.

    Groups where every member already shares the same full BLAKE3 hash
    are silently skipped — those are already surfaced by
    :mod:`compare.exact` and double-emitting them would over-report
    reclaimable bytes.
    """
    images: list[tuple[HashedRecord, str]] = []
    for rec in records:
        if rec.is_archive_member:
            continue
        # Cloud paths are opaque strings; the local read path here has
        # no equivalent on the source dispatch layer.  v0.7 keeps image
        # near-dup detection local-only — cross-source is a v0.8 concern
        # once the reader path via ``Source.read_bytes`` is wired.
        if rec.source_id != "local":
            continue
        if not is_image_path(rec.path):
            continue
        if rec.size < min_size:
            continue
        phash = _load_or_compute_phash(rec, store)
        if phash is None:
            continue
        images.append((rec, phash))

    if len(images) < 2:
        return []

    n = len(images)
    parent = list(range(n))
    size = [1] * n

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        if size[ra] < size[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        size[ra] += size[rb]

    pair_distances: dict[tuple[int, int], int] = {}
    for i in range(n):
        for j in range(i + 1, n):
            d = hamming_distance(images[i][1], images[j][1])
            if d > distance_threshold:
                continue
            pair_distances[(i, j)] = d
            _union(i, j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[_find(i)].append(i)

    out: list[ImageNearDupGroup] = []
    for members_idx in components.values():
        if len(members_idx) < 2:
            continue
        members_idx.sort()
        member_records = [images[i][0] for i in members_idx]
        member_phashes = [images[i][1] for i in members_idx]

        # Skip when the component is already an exact-duplicate group —
        # avoid double-counting against the ``compare.exact`` output.
        full_hashes = {m.full_hash for m in member_records}
        if len(full_hashes) == 1:
            continue

        # Skip components with any member below ``min_size``.  The intake
        # filter above already drops such records, so this branch guards
        # against a caller passing a permissive intake budget but tight
        # group bound.  Symmetric to the tree aggregator's cohesion rail.
        smallest = min(m.size for m in member_records)
        if smallest < min_size:
            continue

        idx_set = set(members_idx)
        component_distances = [
            d for (a, b), d in pair_distances.items() if a in idx_set and b in idx_set
        ]
        # Worst-case Hamming distance gives an honest floor for cluster
        # tightness — matches the tree aggregator's ``min_sim`` shape.
        worst = max(component_distances) if component_distances else 0
        largest = max(m.size for m in member_records)
        out.append(
            ImageNearDupGroup(
                members=member_records,
                max_distance=worst,
                size_range=(smallest, largest),
                phashes=member_phashes,
            )
        )
    return out
