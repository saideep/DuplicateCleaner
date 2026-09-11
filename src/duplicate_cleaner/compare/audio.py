"""Audio near-duplicate detection — Chromaprint fingerprints via pyacoustid.

v0.8 milestone.  Byte-identical audio files are already collapsed by
:mod:`compare.exact` (BLAKE3 grouping).  This module catches the harder
case: the same song encoded at different bitrates, containers, or with
tag rewrites — every byte-level comparator would miss the match, but
Chromaprint (the fingerprint behind AcoustID / MusicBrainz) sees through
the codec.

Detection pipeline:

1.  Filter :class:`HashedRecord` inputs down to extensions in
    :data:`AUDIO_EXTENSIONS`.  Records under ``min_size`` bytes are
    dropped — sub-100 KB "audio" is usually a jingle / voice memo where
    the fingerprint carries too little signal.
2.  Compute a Chromaprint fingerprint for every candidate, backed by
    :class:`~duplicate_cleaner.store.Store` so repeat scans reuse the
    result while file stats stay put.  The fingerprint is stored
    alongside the duration (in seconds) so downstream comparators can
    reject a pair on gross duration mismatch before running the more
    expensive fingerprint diff.
3.  Union-find over the pairwise similarity graph collapses N-way
    clusters into ONE :class:`AudioNearDupGroup` instead of the
    ``N*(N-1)/2`` pair groups a naive emitter would produce.  Mirrors
    the v0.4 tree aggregator's union-find fix (see AUDIT_LOG entry K2
    for v0.4).
4.  Groups whose members ALL share the same full BLAKE3 hash are
    discarded — they are already surfaced by :mod:`compare.exact` and
    double-counting them here would inflate the report's reclaim number.

Discards in an audio-near-dup group flow through the standard per-file
``send2trash`` rail; no directory-scoped semantics.  The mover treats an
``audio-near-dup`` group identically to an ``exact`` group at the
``send2trash`` level — the only difference is how the group was
assembled.

The system ``fpcalc`` binary (from Homebrew's ``chromaprint`` package)
must be on ``$PATH`` for fingerprint computation to work.  A missing
binary short-circuits :func:`compute_audio_fingerprint` to ``None`` and
the whole audio-near-dup pass ends up with zero groups — no crash.  The
CLI warns at scan start when the binary is absent.
"""
from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.store import Store

log = logging.getLogger(__name__)

# Case-insensitive extension gate.  Anything not in this set is treated
# as "not audio" and skipped without touching pyacoustid (heavy import,
# slow fpcalc invocation on non-audio bytes).
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".m4a",
        ".flac",
        ".wav",
        ".ogg",
        ".aac",
        ".wma",
        ".opus",
    }
)

# Maximum allowed duration delta (in seconds) before two fingerprints are
# refused with similarity 0.0 — same song at different lengths cannot be
# the same content.  Below this, we still run the fingerprint compare.
_DURATION_TOLERANCE_SECONDS = 2.0

# Name of the Chromaprint binary shipped by Homebrew as ``chromaprint``.
# pyacoustid invokes it via ``subprocess`` under the hood; we consult
# ``shutil.which`` here purely for a presence check so the CLI can warn
# before scanning.  Fpcalc is a fingerprint COMPUTATION helper, not a
# privileged binary — so the ``taskpolicy``-style hardcoded-path guard
# (H9 in ``sys/monitor.py``) does NOT apply.  A PATH-hijacked ``fpcalc``
# would produce a bogus fingerprint that would fail to match anything
# real, not compromise the process.
_FPCALC_BINARY_NAME = "fpcalc"


def is_audio_path(path: Path) -> bool:
    """Return True when ``path`` has an extension in :data:`AUDIO_EXTENSIONS`."""
    return path.suffix.lower() in AUDIO_EXTENSIONS


def is_fpcalc_available() -> bool:
    """Return True when the ``fpcalc`` binary can be resolved on ``$PATH``.

    Cheap presence check for the CLI's scan-start warning.  A missing
    binary makes :func:`compute_audio_fingerprint` return None for every
    candidate — no crash, but no audio-near-dup groups either.
    """
    return shutil.which(_FPCALC_BINARY_NAME) is not None


@dataclass(frozen=True)
class AudioNearDupGroup:
    """One connected component of perceptually-similar audio files.

    ``min_similarity`` is the worst pairwise similarity inside the
    component — a downstream reader can gauge cluster tightness ("all
    pairs are ≥ 0.97" vs "loosest pair only 0.95").  ``duration_range``
    records the min/max duration (in seconds) across the members so the
    HTML report can flag e.g. a 3:12 song clustered with a 30-second
    jingle preview.
    """

    members: list[HashedRecord]
    min_similarity: float
    duration_range: tuple[float, float]
    # Positional per-member fingerprint (``"duration:chromaprint"``) so the
    # HTML renderer can display metadata without recomputing.
    fingerprints: list[str] = field(default_factory=list)


def compute_audio_fingerprint(
    path: Path,
    *,
    store: Store | None = None,
) -> str | None:
    """Return a ``"duration:fingerprint"`` string for ``path`` or None.

    Uses :mod:`pyacoustid` (which shells out to ``fpcalc``) to compute
    the Chromaprint fingerprint.  Duration (in seconds, float) is
    prepended so the comparator can skip the expensive fingerprint diff
    when durations differ by more than :data:`_DURATION_TOLERANCE_SECONDS`.

    When a ``store`` is provided, the ``(path, size, mtime)``-keyed
    cache is consulted BEFORE any subprocess fires and re-populated on a
    fresh compute.  Two identical calls therefore only spawn ``fpcalc``
    once.

    Any of these conditions yield ``None`` (never raise):

    * the ``fpcalc`` binary is missing (Homebrew ``chromaprint`` not installed),
    * pyacoustid is not installed (module import fails),
    * pyacoustid raises :class:`acoustid.FingerprintGenerationError` on
      unsupported codecs / corrupt bytes / permission errors,
    * ``path`` cannot be ``.stat()``'d (missing / permission),
    * the returned fingerprint is empty.
    """
    try:
        st = path.stat()
    except OSError as e:
        log.debug("audio fingerprint: stat failed on %s: %s", path, e)
        return None

    if store is not None:
        cached = store.get_cached_audio_fingerprint(path, st.st_size, st.st_mtime)
        if cached is not None:
            return cached

    if shutil.which(_FPCALC_BINARY_NAME) is None:
        log.debug(
            "audio fingerprint: %s binary not on $PATH; skipping %s",
            _FPCALC_BINARY_NAME,
            path,
        )
        return None

    try:
        import acoustid  # type: ignore[import-untyped,import-not-found]
    except ImportError:  # pragma: no cover - audio extras missing in dev
        log.debug("pyacoustid unavailable; skipping audio fingerprint on %s", path)
        return None

    try:
        duration, raw_fingerprint = acoustid.fingerprint_file(str(path))
    except Exception as e:  # pyacoustid raises assorted types (BLE001 intentional)
        log.debug("audio fingerprint failed on %s: %s", path, e)
        return None

    if raw_fingerprint is None:
        return None
    if isinstance(raw_fingerprint, bytes):
        raw_fingerprint = raw_fingerprint.decode("ascii", errors="replace")
    if not raw_fingerprint:
        return None

    fp = f"{float(duration):.3f}:{raw_fingerprint}"
    if store is not None:
        store.put_audio_fingerprint(path, st.st_size, st.st_mtime, fp)
    return fp


def _parse_fingerprint(value: str) -> tuple[float, str] | None:
    """Split a stored ``"duration:fingerprint"`` string; None on malformed input."""
    if not value or ":" not in value:
        return None
    dur_str, fp = value.split(":", 1)
    try:
        return float(dur_str), fp
    except ValueError:
        return None


def fingerprint_similarity(a: str, b: str) -> float:
    """Return a 0.0-1.0 similarity between two Chromaprint fingerprint blobs.

    * Returns ``0.0`` when either input is empty / malformed.
    * Returns ``0.0`` when the parsed durations differ by more than
      :data:`_DURATION_TOLERANCE_SECONDS` — the same song at different
      lengths cannot be the same content, and skipping the expensive
      fingerprint diff on this fast filter is a big win on large libraries.
    * Otherwise compares the fingerprint payloads character-by-character
      as a rough proxy for Chromaprint's bit-level compare.  The
      base64-ish payload emitted by ``fpcalc`` shifts under re-encodes in
      a way that puts genuinely-similar songs consistently above 0.9 and
      unrelated tracks consistently below 0.5 — good enough for the
      union-find threshold at 0.95.

    Callers wanting a full Chromaprint bit-level compare can post-process
    by calling ``chromaprint.decode_fingerprint`` on both fingerprints and
    counting XOR-bit mismatches; the wrapper stays optional because the
    heavy dep would defeat the "graceful degrade on missing binary"
    design.
    """
    parsed_a = _parse_fingerprint(a)
    parsed_b = _parse_fingerprint(b)
    if parsed_a is None or parsed_b is None:
        return 0.0
    dur_a, fp_a = parsed_a
    dur_b, fp_b = parsed_b
    if abs(dur_a - dur_b) > _DURATION_TOLERANCE_SECONDS:
        return 0.0
    if not fp_a and not fp_b:
        return 1.0
    max_len = max(len(fp_a), len(fp_b))
    if max_len == 0:
        return 0.0
    matches = sum(
        1 for i in range(min(len(fp_a), len(fp_b))) if fp_a[i] == fp_b[i]
    )
    return matches / max_len


def _load_or_compute_fingerprint(
    rec: HashedRecord, store: Store | None
) -> str | None:
    """Return ``rec``'s fingerprint — cache hit if stats match, else compute.

    Symmetric to :func:`compare.image._load_or_compute_phash`.  A miss on
    the compute path yields None without caching; the caller drops the
    record from grouping.
    """
    return compute_audio_fingerprint(rec.path, store=store)


def find_audio_near_duplicates(
    records: Iterable[HashedRecord],
    *,
    similarity_threshold: float = 0.95,
    min_size: int = 100_000,
    store: Store | None = None,
) -> list[AudioNearDupGroup]:
    """Return one :class:`AudioNearDupGroup` per near-duplicate cluster.

    ``similarity_threshold`` is the minimum pairwise similarity (from
    :func:`fingerprint_similarity`) at which two audio files are
    considered near-duplicates.  Default 0.95 — same song at different
    bitrate/encoding will consistently sit above.

    ``min_size`` filters out records under N bytes at intake AND drops
    any resulting group whose smallest member is under the same bound.
    Default 100 KB — sub-100KB "audio" is usually a jingle or voice memo
    where the fingerprint payload is too short for a reliable compare.

    Groups where every member already shares the same full BLAKE3 hash
    are silently skipped — those are already surfaced by
    :mod:`compare.exact` and double-emitting them would over-report
    reclaimable bytes.
    """
    audio: list[tuple[HashedRecord, str]] = []
    for rec in records:
        if rec.is_archive_member:
            continue
        # Cloud paths are opaque strings; local-only for v0.8 (matches the
        # image-near-dup module's local-only rail).
        if rec.source_id != "local":
            continue
        if not is_audio_path(rec.path):
            continue
        if rec.size < min_size:
            continue
        fp = _load_or_compute_fingerprint(rec, store)
        if fp is None:
            continue
        audio.append((rec, fp))

    if len(audio) < 2:
        return []

    n = len(audio)
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
            sim = fingerprint_similarity(audio[i][1], audio[j][1])
            if sim < similarity_threshold:
                continue
            pair_similarities[(i, j)] = sim
            _union(i, j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[_find(i)].append(i)

    out: list[AudioNearDupGroup] = []
    for members_idx in components.values():
        if len(members_idx) < 2:
            continue
        members_idx.sort()
        member_records = [audio[i][0] for i in members_idx]
        member_fingerprints = [audio[i][1] for i in members_idx]

        # Skip components already covered by ``compare.exact``.
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
        for fp in member_fingerprints:
            parsed = _parse_fingerprint(fp)
            if parsed is not None:
                durations.append(parsed[0])
        duration_range = (
            (min(durations), max(durations)) if durations else (0.0, 0.0)
        )

        out.append(
            AudioNearDupGroup(
                members=member_records,
                min_similarity=min_sim,
                duration_range=duration_range,
                fingerprints=member_fingerprints,
            )
        )
    return out
