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

**Path safety** (H9-style rail, audit pass 17):  the ``fpcalc`` binary
path is HARDCODED to :data:`_FPCALC_PRIMARY_PATH` /
:data:`_FPCALC_FALLBACK_PATH` and NEVER resolved through ``$PATH``.
Pyacoustid honours the ``FPCALC`` environment variable, so we stamp it
to the vetted absolute path BEFORE importing the module.  An attacker
with write access to an early PATH entry (``~/bin``,
``/opt/homebrew/bin``, …) therefore cannot inject a shim that runs with
this process's UID during ``dc scan``.  Same rail as
:mod:`compare.video` (ffmpeg) and :func:`sys.monitor.be_polite`
(``/usr/bin/taskpolicy``).  This module deliberately does NOT
``import shutil`` — the presence check consults ``os.path.exists`` on
the two hardcoded paths only.
"""
from __future__ import annotations

import logging
import os
import subprocess
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

# ------------------------------------------------------------------ #
# Hardcoded fpcalc binary paths — never trust $PATH (H9 invariant).  #
# Same rail as compare/video.py's ffmpeg lookup.  Pyacoustid honours  #
# the FPCALC env var, so we stamp it to the resolved absolute path   #
# BEFORE importing acoustid — pyacoustid's internal subprocess call  #
# then routes through the vetted binary rather than $PATH.           #
# ------------------------------------------------------------------ #
_FPCALC_PRIMARY_PATH: str = "/opt/homebrew/bin/fpcalc"
_FPCALC_FALLBACK_PATH: str = "/usr/local/bin/fpcalc"


def is_audio_path(path: Path) -> bool:
    """Return True when ``path`` has an extension in :data:`AUDIO_EXTENSIONS`."""
    return path.suffix.lower() in AUDIO_EXTENSIONS


def _find_fpcalc() -> str | None:
    """Return the first hardcoded fpcalc path that exists on disk, or None.

    Never falls back to ``shutil.which`` — a PATH-hijacked ``~/bin/fpcalc``
    would execute with the scan process's UID and could exfiltrate the
    audio bytes pyacoustid hands it.  See H9 invariant in AUDIT_LOG.
    """
    for candidate in (_FPCALC_PRIMARY_PATH, _FPCALC_FALLBACK_PATH):
        if os.path.exists(candidate):
            return candidate
    return None


def is_fpcalc_available() -> bool:
    """True when a hardcoded fpcalc absolute path resolves on disk.

    Cheap presence check for the CLI's scan-start warning.  A missing
    binary makes :func:`compute_audio_fingerprint` return None for every
    candidate — no crash, but no audio-near-dup groups either.
    """
    return _find_fpcalc() is not None


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

    fpcalc_path = _find_fpcalc()
    if fpcalc_path is None:
        log.debug(
            "audio fingerprint: fpcalc absent at %s and %s; skipping %s",
            _FPCALC_PRIMARY_PATH,
            _FPCALC_FALLBACK_PATH,
            path,
        )
        return None

    # Route pyacoustid's internal subprocess through the vetted absolute
    # path.  The ``FPCALC`` env var must be set BEFORE ``import acoustid``
    # so pyacoustid's module-load-time lookup picks it up rather than
    # falling back to $PATH.
    os.environ["FPCALC"] = fpcalc_path

    try:
        import acoustid  # type: ignore[import-untyped,import-not-found]
    except ImportError:  # pragma: no cover - audio extras missing in dev
        log.debug("pyacoustid unavailable; skipping audio fingerprint on %s", path)
        return None

    try:
        duration, raw_fingerprint = acoustid.fingerprint_file(str(path))
    except (
        acoustid.FingerprintGenerationError,
        OSError,
        subprocess.SubprocessError,
    ) as e:
        # Expected error modes: unsupported codec, corrupt bytes, missing
        # binary, permission denied, subprocess timeout.  Anything else is
        # a genuine bug (broken pyacoustid install, memory error on huge
        # file, ...) and MUST propagate so the operator sees it instead of
        # silently getting zero audio-near-dup groups.
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


def _decode_fingerprint(fp: str) -> list[int] | None:
    """Decode a base64 Chromaprint payload to a list of uint32 samples.

    Returns None when pyacoustid / chromaprint is unavailable or the
    payload cannot be decoded.  The returned list is safe to XOR
    positionally against another decoded fingerprint of matching length
    for a bit-level Hamming compare.
    """
    if not fp:
        return None
    try:
        from acoustid import (  # type: ignore[import-untyped,import-not-found]
            chromaprint,
        )
    except ImportError:  # pragma: no cover - audio extras missing in dev
        return None
    try:
        decoded, _algorithm = chromaprint.decode_fingerprint(fp.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return None
    if not decoded:
        return None
    return list(decoded)


def fingerprint_similarity(a: str, b: str) -> float:
    """Return a 0.0-1.0 similarity between two Chromaprint fingerprint blobs.

    * Returns ``0.0`` when either input is empty / malformed.
    * Returns ``0.0`` when the parsed durations differ by more than
      :data:`_DURATION_TOLERANCE_SECONDS` — the same song at different
      lengths cannot be the same content, and skipping the expensive
      fingerprint diff on this fast filter is a big win on large libraries.
    * Otherwise decodes both payloads via
      :func:`acoustid.chromaprint.decode_fingerprint` to uint32 arrays and
      computes a bit-level Hamming similarity across positional samples.
      A bit-distance of 0 across every sample → similarity 1.0; a
      bit-distance of 32 per sample (random bit noise) → similarity 0.5.
      This is the same compare algorithm AcoustID's own matcher uses.

    Threshold interpretation: 0.95 corresponds to ~5% bit distance across
    the decoded fingerprint — same song re-encoded at a different bitrate
    consistently sits at 0.95+; unrelated tracks sit near 0.5.
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
    decoded_a = _decode_fingerprint(fp_a)
    decoded_b = _decode_fingerprint(fp_b)
    if decoded_a is None or decoded_b is None:
        return 0.0
    common = min(len(decoded_a), len(decoded_b))
    if common == 0:
        return 0.0
    total_bits = common * 32
    diff_bits = 0
    for i in range(common):
        diff_bits += bin(decoded_a[i] ^ decoded_b[i]).count("1")
    similarity = 1.0 - (diff_bits / total_bits)
    if similarity < 0.0:
        return 0.0
    if similarity > 1.0:
        return 1.0
    return similarity


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
        fp = compute_audio_fingerprint(rec.path, store=store)
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
