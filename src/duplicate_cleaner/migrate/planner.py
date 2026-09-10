"""Migration planner — enumerate source, decide per-file action, write plan.

v0.5-a: read-only.  ``plan_migration`` walks the source's ``list_files()``,
classifies every eligible file into copy / skip / defer / error, and returns
a :class:`MigrationPlan`.  The paired :mod:`duplicate_cleaner.migrate.render`
module writes the plan JSON + HTML.

No network writes fire at plan time.  The destination is consulted read-only
(``list_files()`` + hash-index build) to skip files already present at the
target path with a matching size + hash.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from duplicate_cleaner.migrate.plan import (
    MigrationEntry,
    MigrationFilter,
    MigrationPlan,
    dest_size_limit_for,
)

if TYPE_CHECKING:
    from duplicate_cleaner.scan.walk import FileRecord
    from duplicate_cleaner.sources.base import Source

log = logging.getLogger(__name__)

# Google-native MIME prefix — matches ``sources.gdrive._GOOGLE_NATIVE_MIME_PREFIX``.
# Copied here so the planner does not reach into the gdrive module's private
# state.  v0.5-b's copy step will re-check the same prefix defensively.
_GOOGLE_NATIVE_MIME_PREFIX = "application/vnd.google-apps."


def plan_migration(
    source: Source,
    dest: Source,
    *,
    filter_: MigrationFilter | None = None,
    dest_size_limit_gb: float | None = None,
) -> MigrationPlan:
    """Enumerate ``source`` and return a :class:`MigrationPlan`.

    ``filter_`` restricts the plan to a subset of the source's files.
    Include/exclude globs match against the source path's POSIX form; size
    bounds are inclusive on the low end, exclusive on the high end.

    ``dest_size_limit_gb`` overrides the provider-derived per-file limit
    (Google 5 TB, OneDrive 250 GB) — expressed in gigabytes so the CLI can
    surface it as a human-friendly knob.  ``None`` uses the provider
    default.

    Any file whose size exceeds the per-file limit surfaces as
    ``action="error"`` with ``size_limit_hit=True`` so the plan reader can
    triage them separately from copy-eligible entries.

    Refuses self-copy (``source.id == dest.id``) — same-account migration
    never moves bytes across an ownership boundary and every downstream
    tripwire assumes distinct ids.  Enforced here (planner) plus mirrored
    in :func:`execute_migration` for defense-in-depth.
    """
    if source.id == dest.id:
        raise ValueError(
            f"source_id and dest_id must differ; migration cannot be "
            f"same-account (both are {source.id!r})."
        )
    filt = filter_ if filter_ is not None else MigrationFilter()
    # Resolve the destination per-file cap.  The CLI hands us a GB float
    # (or None); we translate to bytes here.  A provider-derived default
    # of ``None`` (unknown destination type) is left as ``None`` so no
    # error-branch fires for entries we can't gate.
    dest_cap_bytes: int | None
    if dest_size_limit_gb is not None:
        dest_cap_bytes = int(dest_size_limit_gb * 1024 * 1024 * 1024)
    else:
        dest_cap_bytes = dest_size_limit_for(dest.id)

    # Build a lightweight (dest_path -> (size, foreign_hash)) index over
    # the destination so the "already present" skip decision is O(1) per
    # source entry.  We consult ``dest.list_files()`` once and hold the
    # index in memory — v0.5-a plans are single-run artifacts, and the
    # dest is only enumerated for equality checks (never mutated).
    dest_index = _build_dest_index(dest)

    entries: list[MigrationEntry] = []
    for rec in source.list_files():
        entry = _classify(rec, filt, dest_index, dest_cap_bytes)
        if entry is None:
            continue
        entries.append(entry)
    plan = MigrationPlan(
        source_id=source.id,
        dest_id=dest.id,
        entries=entries,
        filter_summary=_filter_summary(filt, dest_cap_bytes),
    )
    return plan


def _build_dest_index(
    dest: Source,
) -> dict[str, tuple[int, str | None]]:
    """Index the destination by ``str(dest_relative_path)``.

    Returns ``{dest_relative_posix_path: (size, foreign_hash)}``.  The
    destination's ``list_files()`` yields ``FileRecord``s whose ``path``
    is the same ``<source_id>://<posix>`` virtual URI shape that
    ``_dest_expected_path_for`` produces.  We strip the ``<source_id>://``
    prefix to key on the provider-relative path only.
    """
    out: dict[str, tuple[int, str | None]] = {}
    try:
        for rec in dest.list_files():
            key = _strip_source_scheme(str(rec.path), dest.id)
            if key:
                out[key] = (int(rec.size), rec.foreign_hash)
    except Exception as exc:
        log.warning(
            "Could not fully enumerate destination %s for skip-index: %s",
            dest.id,
            exc,
        )
    return out


def _strip_source_scheme(path_str: str, source_id: str) -> str:
    """Return ``path_str`` with the ``<source_id>://`` prefix removed.

    Cloud ``FileRecord.path`` is stamped as ``f"{source_id}://{drive_path}"``.
    Path normalises ``//`` to ``/`` on some platforms, so we accept both
    ``<id>://`` and ``<id>:/`` prefixes defensively.
    """
    prefix_double = f"{source_id}://"
    prefix_single = f"{source_id}:/"
    if path_str.startswith(prefix_double):
        return path_str[len(prefix_double):]
    if path_str.startswith(prefix_single):
        return path_str[len(prefix_single):]
    return path_str


def _classify(
    rec: FileRecord,
    filt: MigrationFilter,
    dest_index: dict[str, tuple[int, str | None]],
    dest_cap_bytes: int | None,
) -> MigrationEntry | None:
    """Return the plan entry for ``rec``, or ``None`` when filtered out.

    The action decision follows the design contract:

    1. Shared cloud files → ``defer`` (informational-only invariant).
    2. Google-native docs → ``defer`` (no downloadable bytes).
    3. Filter globs / size bounds → silent drop (``None``).
    4. Over-cap for destination → ``error`` with ``size_limit_hit=True``.
    5. Present at dest with matching size + hash → ``skip``.
    6. Otherwise → ``copy``.
    """
    source_path = _display_path_for(rec)
    # 1. Shared cloud files are informational-only per v0.2 invariant.  A
    # copy would introduce a second live edit surface for someone else's
    # bytes and confuse ownership.
    if filt.exclude_shared and rec.is_shared:
        return MigrationEntry(
            source_id=rec.source_id,
            source_file_id=rec.cloud_file_id,
            source_path=source_path,
            source_etag=rec.etag,
            source_size=rec.size,
            source_hash=rec.foreign_hash,
            source_mime=None,
            dest_expected_path=_dest_expected_path_for(rec),
            action="defer",
            reason="shared with me — informational-only",
        )
    # 2. Google-native docs export at the boundary and have no
    # downloadable bytes.  v0.5-a defers them; v0.5-b or later may add
    # export-as-{docx,xlsx,pptx} support.
    mime = _mime_of(rec)
    if filt.exclude_google_native and mime.startswith(_GOOGLE_NATIVE_MIME_PREFIX):
        return MigrationEntry(
            source_id=rec.source_id,
            source_file_id=rec.cloud_file_id,
            source_path=source_path,
            source_etag=rec.etag,
            source_size=rec.size,
            source_hash=rec.foreign_hash,
            source_mime=mime or None,
            dest_expected_path=_dest_expected_path_for(rec),
            action="defer",
            reason="Google-native (no downloadable bytes)",
        )
    # 3. User filters — silent drop.
    if filt.include_globs and not any(
        fnmatch.fnmatch(source_path, g) for g in filt.include_globs
    ):
        return None
    if filt.exclude_globs and any(
        fnmatch.fnmatch(source_path, g) for g in filt.exclude_globs
    ):
        return None
    if rec.size < filt.min_size:
        return None
    if filt.max_size is not None and rec.size > filt.max_size:
        return None
    # 4. Destination per-file cap — surfaces loudly.
    if dest_cap_bytes is not None and rec.size > dest_cap_bytes:
        return MigrationEntry(
            source_id=rec.source_id,
            source_file_id=rec.cloud_file_id,
            source_path=source_path,
            source_etag=rec.etag,
            source_size=rec.size,
            source_hash=rec.foreign_hash,
            source_mime=mime or None,
            dest_expected_path=_dest_expected_path_for(rec),
            action="error",
            reason=(
                f"exceeds destination per-file limit "
                f"({rec.size} > {dest_cap_bytes} bytes)"
            ),
            size_limit_hit=True,
        )
    # 5. Already present at destination with matching size + hash.
    dest_rel = _dest_expected_path_for(rec)
    dest_hit = dest_index.get(dest_rel)
    if dest_hit is not None:
        dest_size, dest_hash = dest_hit
        if _matches_dest(rec, dest_size, dest_hash):
            return MigrationEntry(
                source_id=rec.source_id,
                source_file_id=rec.cloud_file_id,
                source_path=source_path,
                source_etag=rec.etag,
                source_size=rec.size,
                source_hash=rec.foreign_hash,
                source_mime=mime or None,
                dest_expected_path=dest_rel,
                action="skip",
                reason="already at destination (matching size + hash)",
            )
    # 6. Otherwise — propose the copy.
    return MigrationEntry(
        source_id=rec.source_id,
        source_file_id=rec.cloud_file_id,
        source_path=source_path,
        source_etag=rec.etag,
        source_size=rec.size,
        source_hash=rec.foreign_hash,
        source_mime=mime or None,
        dest_expected_path=dest_rel,
        action="copy",
        reason="copy to destination",
    )


def _matches_dest(
    rec: FileRecord,
    dest_size: int,
    dest_hash: str | None,
) -> bool:
    """Return True when the destination file already carries matching bytes.

    Size is the fast-fail check.  Hash equality is checked only when both
    sides carry a foreign hash AND their algo prefixes match — cross-algo
    (BLAKE3 local vs. MD5 gdrive) can't be compared without a reconcile
    download, which is deferred to v0.5-b's copy step.  Size-only equality
    is intentionally insufficient — a size collision between two different
    files must not trigger a skip.
    """
    if rec.size != dest_size:
        return False
    if not rec.foreign_hash or not dest_hash:
        return False
    src_algo, _, src_hex = rec.foreign_hash.partition(":")
    dst_algo, _, dst_hex = dest_hash.partition(":")
    if not src_algo or src_algo != dst_algo:
        return False
    return src_hex.lower() == dst_hex.lower()


def _mime_of(rec: FileRecord) -> str:
    """Return the record's provider-side MIME string, empty when unknown.

    ``FileRecord`` does not currently carry a MIME field; the Google
    Drive scanner filters google-native docs out of ``list_files()`` at
    scan time.  A shared/deferred Google-native item does not survive to
    the plan step today.  This helper is a forward-compat seam so a
    future ``FileRecord.mime_type`` field slots in without changing the
    planner's public API.
    """
    return str(getattr(rec, "mime_type", None) or "")


def _display_path_for(rec: FileRecord) -> str:
    """Return the human-facing source path string used in the plan.

    Cloud records already carry a virtual ``<source_id>://…`` path; we
    render it verbatim.  Local records use the resolved absolute path.
    """
    return str(rec.path)


def _dest_expected_path_for(rec: FileRecord) -> str:
    """Compute the destination-relative path v0.5-b will upload to.

    Rename policy: preserve — v0.5's migration never mutates filename
    bytes.  Source directory structure is preserved (``My Drive/Photos/
    2024/foo.jpg`` uploads to the same relative path under the dest).

    Cloud paths carry the ``<source_id>://`` scheme prefix; we strip it.
    ``PurePosixPath`` normalises redundant slashes without touching the
    provider-side casing.
    """
    raw = _strip_source_scheme(str(rec.path), rec.source_id)
    # ``pathlib`` on macOS collapses ``//`` inside a Path — the cloud
    # virtual path ``gdrive:x://Photos/2024`` gets normalised to
    # ``gdrive:x:/Photos/2024`` on ingest.  ``_strip_source_scheme``
    # handles both shapes; ``PurePosixPath`` here canonicalises the tail.
    normalised = PurePosixPath(raw)
    parts = [p for p in normalised.parts if p not in ("", "/")]
    return "/".join(parts)


def _filter_summary(filt: MigrationFilter, dest_cap_bytes: int | None) -> str:
    """Return a short human-readable summary of the applied filter.

    Rendered into the plan HTML header and stamped into the JSON so a
    reviewer can see the exact filter that produced the entry set without
    consulting the CLI invocation history.
    """
    parts: list[str] = []
    if filt.include_globs:
        parts.append("include=" + ",".join(filt.include_globs))
    if filt.exclude_globs:
        parts.append("exclude=" + ",".join(filt.exclude_globs))
    if filt.min_size:
        parts.append(f"min_size={filt.min_size}")
    if filt.max_size is not None:
        parts.append(f"max_size={filt.max_size}")
    if not filt.exclude_shared:
        parts.append("include_shared=True")
    if dest_cap_bytes is not None:
        parts.append(f"dest_cap_bytes={dest_cap_bytes}")
    return "; ".join(parts) or "no filter"
