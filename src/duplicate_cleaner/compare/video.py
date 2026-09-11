"""Video near-duplicate detection — ffmpeg keyframe pHashes.

v0.8 milestone.  Byte-identical videos are already collapsed by
:mod:`compare.exact`.  This module catches the harder case: the same
video re-encoded (bitrate change, container swap, resolution downscale)
where the byte-level compare fails but the visual content is the same.

Detection pipeline:

1.  Filter :class:`HashedRecord` inputs down to extensions in
    :data:`VIDEO_EXTENSIONS`.  Records under ``min_size`` bytes are
    dropped (default 1 MB — sub-MB video is usually a corrupt fragment).
2.  Shell out to the system ``ffmpeg`` binary — HARDCODED absolute path
    lookup (``/opt/homebrew/bin/ffmpeg`` primary,
    ``/usr/local/bin/ffmpeg`` fallback) — to extract N keyframes evenly
    spaced across the video's duration.  Each keyframe is downscaled to
    32x32 grayscale and pHashed via :func:`imagehash.phash` at
    ``hash_size=16`` (256-bit hash — matches the image pipeline).
    Signature is stored as ``"duration:phash1,phash2,..."`` so the
    comparator can reject a pair on gross duration mismatch before
    running the pairwise Hamming compare, and the similarity score
    normalises by the true bit budget (``256``) so random content sits
    at ≈ 0.5 rather than 0.87 as the earlier ``/256`` +64-bit-hash
    mismatch produced.
3.  Union-find over the pairwise similarity graph collapses N-way
    clusters into ONE :class:`VideoNearDupGroup` — mirrors the v0.4 tree
    aggregator's union-find fix.
4.  Groups whose members ALL share the same full BLAKE3 hash are
    discarded (already covered by :mod:`compare.exact`).

**Path safety** (H9-style rail, audit pass 14):  the ``ffmpeg`` binary
path is HARDCODED to
:data:`_FFMPEG_PRIMARY_PATH` / :data:`_FFMPEG_FALLBACK_PATH` and NEVER
resolved through ``$PATH``.  An attacker with write access to an early
PATH entry (``~/bin``, ``/opt/homebrew/bin``, …) cannot inject a shim
that runs with this process's UID during ``dc scan``.  This is the same
rail :func:`sys.monitor.be_polite` applies to ``/usr/bin/taskpolicy``.

Discards in a video-near-dup group flow through the standard per-file
``send2trash`` rail; no directory-scoped semantics.  The mover treats a
``video-near-dup`` group identically to an ``exact`` group at the
send2trash level — the only difference is how the group was assembled.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.store import Store

log = logging.getLogger(__name__)

# Case-insensitive extension gate.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".wmv",
        ".flv",
        ".m4v",
    }
)

# Number of evenly-spaced keyframes to sample when building the signature.
# 5 gives a decent proxy for scene content without a heavy ffmpeg cost.
_KEYFRAMES: int = 5

# pHash hash_size used for keyframe compares.  16 → 256-bit hash → max
# Hamming distance 256.  Matches :mod:`compare.image`'s pHash budget for
# consistency, and gives the video near-dup pass real resolution — video
# re-encodes have more variation than image re-saves, so tighter bits +
# a matching normaliser make the default threshold discriminate rather
# than trivially cluster unrelated clips.
_PHASH_HASH_SIZE: int = 16

# Bit budget the similarity score normalises against.  Equals the number
# of bits in one keyframe pHash — ``_PHASH_HASH_SIZE * _PHASH_HASH_SIZE``.
# Two random pHashes score ≈ 0.5 (half the bits flipped on average);
# identical pHashes score 1.0.  Kept as a named constant so future
# resolution bumps stay locked to the actual bit count.
_PHASH_BIT_BUDGET: int = _PHASH_HASH_SIZE * _PHASH_HASH_SIZE

# Duration filter — two videos differing by more than N seconds cannot
# be the same content.  Video has more variance across re-encodes than
# audio (chapter markers, trim, …) so the tolerance is bigger.
_DURATION_TOLERANCE_SECONDS: float = 5.0

# ------------------------------------------------------------------ #
# Hardcoded binary paths — never trust $PATH (audit pass 14 finding). #
# ------------------------------------------------------------------ #
_FFMPEG_PRIMARY_PATH: str = "/opt/homebrew/bin/ffmpeg"
_FFMPEG_FALLBACK_PATH: str = "/usr/local/bin/ffmpeg"
_FFPROBE_PRIMARY_PATH: str = "/opt/homebrew/bin/ffprobe"
_FFPROBE_FALLBACK_PATH: str = "/usr/local/bin/ffprobe"


def is_video_path(path: Path) -> bool:
    """Return True when ``path`` has an extension in :data:`VIDEO_EXTENSIONS`."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _resolve_ffmpeg() -> str | None:
    """Return the first hardcoded ffmpeg path that exists on disk, or None."""
    for candidate in (_FFMPEG_PRIMARY_PATH, _FFMPEG_FALLBACK_PATH):
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_ffprobe() -> str | None:
    """Return the first hardcoded ffprobe path that exists on disk, or None."""
    for candidate in (_FFPROBE_PRIMARY_PATH, _FFPROBE_FALLBACK_PATH):
        if os.path.exists(candidate):
            return candidate
    return None


def is_ffmpeg_available() -> bool:
    """True when a hardcoded ffmpeg absolute path resolves on disk."""
    return _resolve_ffmpeg() is not None


@dataclass(frozen=True)
class VideoNearDupGroup:
    """One connected component of perceptually-similar videos.

    ``min_similarity`` is the worst pairwise similarity inside the
    component.  ``duration_range`` records min/max duration (seconds)
    across members so the HTML report can flag a full-length feature
    clustered with a 30-second trailer.
    """

    members: list[HashedRecord]
    min_similarity: float
    duration_range: tuple[float, float]
    signatures: list[str] = field(default_factory=list)


def _probe_duration(path: Path) -> float | None:
    """Return video duration in seconds via ``ffprobe`` — None on failure."""
    binary = _resolve_ffprobe()
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("ffprobe invocation failed on %s: %s", path, e)
        return None
    if result.returncode != 0:
        log.debug(
            "ffprobe returned %d on %s; stderr=%s",
            result.returncode,
            path,
            result.stderr.strip(),
        )
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def _extract_keyframe_phash(
    binary: str, path: Path, timestamp: float, tmp_dir: Path
) -> str | None:
    """Extract a single frame at ``timestamp`` via ffmpeg and return its pHash hex."""
    frame_path = tmp_dir / f"frame-{timestamp:.3f}.png"
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=32:32:force_original_aspect_ratio=disable,format=gray",
                "-y",
                str(frame_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("ffmpeg frame extract failed on %s at %.2fs: %s", path, timestamp, e)
        return None
    if result.returncode != 0 or not frame_path.exists():
        log.debug(
            "ffmpeg returned %d on %s at %.2fs; stderr=%s",
            result.returncode,
            path,
            timestamp,
            result.stderr.strip(),
        )
        return None
    try:
        import imagehash  # type: ignore[import-untyped]
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - image extras missing in dev
        log.debug("imagehash / Pillow unavailable; skipping keyframe pHash")
        return None
    try:
        with Image.open(frame_path) as im:
            phash = imagehash.phash(im, hash_size=_PHASH_HASH_SIZE)
    except (OSError, ValueError, UnidentifiedImageError) as e:
        log.debug("keyframe pHash failed on %s at %.2fs: %s", path, timestamp, e)
        return None
    return str(phash)


def compute_video_signature(
    path: Path,
    *,
    store: Store | None = None,
    keyframes: int = _KEYFRAMES,
) -> str | None:
    """Return a ``"duration:phash1,phash2,..."`` signature for ``path`` or None.

    Extracts ``keyframes`` evenly-spaced frames via the hardcoded
    ``ffmpeg`` binary, downscales each to 32x32 grayscale, and
    perceptual-hashes them.  The result is joined into one string so the
    cache and comparator both handle a scalar value.

    Any of these conditions yield ``None`` (never raise):

    * the ``ffmpeg`` or ``ffprobe`` binary is missing at every hardcoded path,
    * ``ffprobe`` cannot report a duration (unsupported container),
    * ``ffmpeg`` fails to extract any keyframe,
    * imagehash / Pillow is not installed,
    * ``path`` cannot be ``.stat()``'d.

    When a ``store`` is provided, the ``(path, size, mtime)``-keyed
    cache is consulted BEFORE any subprocess fires and re-populated on a
    fresh compute.
    """
    try:
        st = path.stat()
    except OSError as e:
        log.debug("video signature: stat failed on %s: %s", path, e)
        return None

    if store is not None:
        cached = store.get_cached_video_signature(path, st.st_size, st.st_mtime)
        if cached is not None:
            return cached

    binary = _resolve_ffmpeg()
    if binary is None:
        log.debug(
            "video signature: ffmpeg absent at %s and %s; skipping %s",
            _FFMPEG_PRIMARY_PATH,
            _FFMPEG_FALLBACK_PATH,
            path,
        )
        return None

    duration = _probe_duration(path)
    if duration is None or duration <= 0.0:
        return None

    # Evenly-spaced timestamps skipping the very first / very last frame
    # (which are usually black or a title card).
    if keyframes < 1:
        return None
    if keyframes == 1:
        timestamps = [duration / 2.0]
    else:
        step = duration / (keyframes + 1)
        timestamps = [step * (i + 1) for i in range(keyframes)]

    with tempfile.TemporaryDirectory(prefix="dc-video-") as td:
        tmp_dir = Path(td)
        phashes: list[str] = []
        for ts in timestamps:
            phash = _extract_keyframe_phash(binary, path, ts, tmp_dir)
            if phash is None:
                # A single keyframe failure aborts the whole signature —
                # a partial signature would silently reshape the compare.
                return None
            phashes.append(phash)

    signature = f"{duration:.1f}:{','.join(phashes)}"
    if store is not None:
        store.put_video_signature(path, st.st_size, st.st_mtime, signature)
    return signature


def _parse_signature(value: str) -> tuple[float, list[str]] | None:
    """Split a stored signature; None on malformed input."""
    if not value or ":" not in value:
        return None
    dur_str, rest = value.split(":", 1)
    try:
        duration = float(dur_str)
    except ValueError:
        return None
    phashes = [p for p in rest.split(",") if p]
    if not phashes:
        return None
    return duration, phashes


def _hamming(a: str, b: str) -> int | None:
    """Return the bit-distance between two hex pHashes, or None on shape mismatch."""
    if not a or not b or len(a) != len(b):
        return None
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return None


def signature_similarity(a: str, b: str) -> float:
    """Return a 0.0-1.0 similarity between two video signatures.

    * Returns ``0.0`` on empty / malformed input.
    * Returns ``0.0`` when parsed durations differ by more than
      :data:`_DURATION_TOLERANCE_SECONDS`.
    * Otherwise averages the pairwise Hamming distances of the N
      keyframe pHashes (positional) and normalises by the actual bit
      budget (:data:`_PHASH_BIT_BUDGET` = ``_PHASH_HASH_SIZE * _PHASH_HASH_SIZE``).
      Two identical pHashes → 1.0.  Two random pHashes → ≈ 0.5 (half the
      bits flipped on average).  The default threshold at 0.90 therefore
      admits only pairs with < 10% of the bits differing.
    """
    parsed_a = _parse_signature(a)
    parsed_b = _parse_signature(b)
    if parsed_a is None or parsed_b is None:
        return 0.0
    dur_a, phashes_a = parsed_a
    dur_b, phashes_b = parsed_b
    if abs(dur_a - dur_b) > _DURATION_TOLERANCE_SECONDS:
        return 0.0
    if len(phashes_a) != len(phashes_b) or not phashes_a:
        return 0.0
    distances: list[int] = []
    for pa, pb in zip(phashes_a, phashes_b, strict=False):
        d = _hamming(pa, pb)
        if d is None:
            return 0.0
        distances.append(d)
    avg = sum(distances) / float(len(distances))
    similarity = 1.0 - (avg / float(_PHASH_BIT_BUDGET))
    if similarity < 0.0:
        return 0.0
    if similarity > 1.0:
        return 1.0
    return similarity


def find_video_near_duplicates(
    records: Iterable[HashedRecord],
    *,
    similarity_threshold: float = 0.90,
    min_size: int = 1_000_000,
    store: Store | None = None,
) -> list[VideoNearDupGroup]:
    """Return one :class:`VideoNearDupGroup` per near-duplicate cluster.

    ``similarity_threshold`` default 0.90 — video has more variance across
    re-encodes than audio, so the bar is lower.  ``min_size`` default 1 MB
    filters out tiny video fragments where keyframe extraction is unstable.
    Groups whose members already share the same full BLAKE3 hash are
    silently skipped (already covered by :mod:`compare.exact`).
    """
    videos: list[tuple[HashedRecord, str]] = []
    for rec in records:
        if rec.is_archive_member:
            continue
        if rec.source_id != "local":
            continue
        if not is_video_path(rec.path):
            continue
        if rec.size < min_size:
            continue
        signature = compute_video_signature(rec.path, store=store)
        if signature is None:
            continue
        videos.append((rec, signature))

    if len(videos) < 2:
        return []

    n = len(videos)
    parent = list(range(n))
    size_uf = [1] * n

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        if size_uf[ra] < size_uf[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        size_uf[ra] += size_uf[rb]

    pair_similarities: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = signature_similarity(videos[i][1], videos[j][1])
            if sim < similarity_threshold:
                continue
            pair_similarities[(i, j)] = sim
            _union(i, j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[_find(i)].append(i)

    out: list[VideoNearDupGroup] = []
    for members_idx in components.values():
        if len(members_idx) < 2:
            continue
        members_idx.sort()
        member_records = [videos[i][0] for i in members_idx]
        member_signatures = [videos[i][1] for i in members_idx]

        full_hashes = {m.full_hash for m in member_records}
        if len(full_hashes) == 1:
            continue

        smallest = min(m.size for m in member_records)
        if smallest < min_size:
            continue

        idx_set = set(members_idx)
        component_sims = [
            s for (a, b), s in pair_similarities.items() if a in idx_set and b in idx_set
        ]
        min_sim = min(component_sims) if component_sims else 1.0

        durations: list[float] = []
        for sig in member_signatures:
            parsed = _parse_signature(sig)
            if parsed is not None:
                durations.append(parsed[0])
        duration_range = (
            (min(durations), max(durations)) if durations else (0.0, 0.0)
        )

        out.append(
            VideoNearDupGroup(
                members=member_records,
                min_similarity=min_sim,
                duration_range=duration_range,
                signatures=member_signatures,
            )
        )
    return out
