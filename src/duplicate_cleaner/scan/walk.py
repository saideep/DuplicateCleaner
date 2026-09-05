"""Filesystem walk — yields FileRecords, applies hard-coded and user exclusions."""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.constants import EXCLUDED_DIR_NAMES, EXCLUDED_FILE_SUFFIXES
from duplicate_cleaner.paths import (
    is_excluded_root_path,
    matches_globs,
    resolve_for_check,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRecord:
    """Stat snapshot of one candidate file.

    ``is_archive_member`` marks records synthesized from archive contents —
    their ``path`` is a virtual ``outer.zip::inner`` string, they have no
    real inode/dev, and the mover refuses to trash them individually.
    ``is_bundle`` marks the tree-hash of a macOS bundle rolled up to a
    single record.
    """

    path: Path
    size: int
    mtime: float
    inode: int
    dev: int
    nlink: int
    is_archive_member: bool = False
    is_bundle: bool = False
    precomputed_full_hash: str | None = None


@dataclass
class WalkStats:
    """Side-channel counters the walker fills in as it iterates.

    Callers create one, hand it to :func:`iter_files`, and read the count
    at the end. Kept out of the yielded record type so a plain iterator
    consumer never has to know about scan-wide totals.
    """

    files_visited: int = 0
    bundles_hashed: int = 0
    dirs_visited: int = 0
    archive_skips: list[dict[str, str]] = field(default_factory=list)


def iter_files(
    roots: list[Path],
    *,
    follow_symlinks: bool = False,
    exclude_globs: list[str] | None = None,
    min_size_bytes: int = 0,
    bundle_extensions: tuple[str, ...] | list[str] | None = None,
    stats: WalkStats | None = None,
) -> Iterator[FileRecord]:
    """Walk each root, yielding FileRecords for eligible regular files.

    ``bundle_extensions`` (dot-prefixed, lower-cased) causes any directory
    whose name ends with one of them to be hashed as a single atomic unit
    instead of descended into. Default is no bundle handling — callers pass
    the config's list explicitly.
    """
    globs = exclude_globs or []
    bundles = _normalise_bundle_exts(bundle_extensions)
    seen_roots: set[Path] = set()
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        if root in seen_roots:
            continue
        seen_roots.add(root)
        if not root.exists():
            log.warning("Skipping missing root: %s", root)
            continue
        yield from _walk(root, follow_symlinks, globs, min_size_bytes, bundles, stats)


def _normalise_bundle_exts(
    v: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    if not v:
        return ()
    out: list[str] = []
    for raw in v:
        s = raw.strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        out.append(s)
    return tuple(out)


def _walk(
    root: Path,
    follow_symlinks: bool,
    globs: list[str],
    min_size_bytes: int,
    bundle_exts: tuple[str, ...],
    stats: WalkStats | None,
) -> Iterator[FileRecord]:
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        # Always check both the raw and physically-resolved path so a symlink
        # into an excluded root cannot bypass the check.
        if is_excluded_root_path(current) or is_excluded_root_path(
            resolve_for_check(current)
        ):
            continue
        try:
            scandir_ctx = os.scandir(current)
        except OSError as e:
            log.debug("scandir failed on %s: %s", current, e)
            continue
        if stats is not None:
            stats.dirs_visited += 1
        with scandir_ctx as it:
            for entry in it:
                rec = _process_entry(
                    entry,
                    stack,
                    follow_symlinks,
                    globs,
                    min_size_bytes,
                    bundle_exts,
                    stats,
                )
                if rec is not None:
                    if stats is not None:
                        stats.files_visited += 1
                    yield rec


def _process_entry(
    entry: os.DirEntry[str],
    stack: list[Path],
    follow_symlinks: bool,
    globs: list[str],
    min_size_bytes: int,
    bundle_exts: tuple[str, ...],
    stats: WalkStats | None,
) -> FileRecord | None:
    try:
        p = Path(entry.path)
        name = entry.name
        is_symlink = entry.is_symlink()
        if is_symlink and not follow_symlinks:
            return None

        # Resolve the physical path for exclusion checks (catches symlinks
        # into ~/Library, /System, etc.). Yield the un-resolved path so the
        # report reads naturally.
        resolved = resolve_for_check(p) if is_symlink else p

        if entry.is_dir(follow_symlinks=follow_symlinks):
            if name in EXCLUDED_DIR_NAMES:
                return None
            if is_excluded_root_path(p) or is_excluded_root_path(resolved):
                return None
            if matches_globs(p, globs):
                return None
            if _is_bundle_dir(name, bundle_exts):
                # Hash the whole tree as one unit; do NOT descend into it.
                rec = _hash_bundle(p, min_size_bytes, follow_symlinks)
                if rec is not None and stats is not None:
                    stats.bundles_hashed += 1
                return rec
            stack.append(p)
            return None
        if not entry.is_file(follow_symlinks=follow_symlinks):
            return None
        if name.endswith(EXCLUDED_FILE_SUFFIXES):
            return None
        # File-level exclusion: symlinked file pointing into excluded root.
        if is_excluded_root_path(p) or is_excluded_root_path(resolved):
            return None
        if matches_globs(p, globs):
            return None
        st = entry.stat(follow_symlinks=follow_symlinks)
        if st.st_size < min_size_bytes:
            return None
        return FileRecord(
            path=p,
            size=st.st_size,
            mtime=st.st_mtime,
            inode=st.st_ino,
            dev=st.st_dev,
            nlink=st.st_nlink,
        )
    except OSError as e:
        log.debug("Skip entry %s: %s", entry.path, e)
        return None


def _is_bundle_dir(name: str, bundle_exts: tuple[str, ...]) -> bool:
    if not bundle_exts:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in bundle_exts)


def _hash_bundle(
    bundle_path: Path,
    min_size_bytes: int,
    follow_symlinks: bool,
) -> FileRecord | None:
    """Roll a bundle directory tree up into a single FileRecord.

    Content hash = BLAKE3 over the sorted ``(relative_path, size,
    content_hash)`` triples so ordering differences between filesystems
    don't split otherwise-identical bundles. Individual member content is
    hashed with BLAKE3 too, streaming to keep memory flat.

    H8: with ``follow_symlinks=True``, ``os.walk`` still follows symlinks
    on the file level even without ``followlinks`` — and a malicious
    ``.app`` could contain a symlink pointing outside the bundle (e.g. at
    ``~/.ssh/id_rsa``). We validate every member's resolved path stays
    inside the resolved bundle root; escaping symlinks are skipped with a
    debug log and never open ``fp.open('rb')`` on the target.
    """
    triples: list[tuple[str, int, str]] = []
    total_size = 0
    latest_mtime = 0.0
    try:
        st_root = bundle_path.stat()
    except OSError as exc:
        log.debug("Cannot stat bundle %s: %s", bundle_path, exc)
        return None
    resolved_bundle_root = resolve_for_check(bundle_path)
    for sub_root, dirs, files in os.walk(bundle_path, followlinks=follow_symlinks):
        # Sorted walk so the assembly hash is deterministic irrespective of
        # inode order.
        dirs.sort()
        files.sort()
        sub_root_path = Path(sub_root)
        for fname in files:
            fp = sub_root_path / fname
            try:
                if fp.is_symlink() and not follow_symlinks:
                    continue
                # H8: even when ``follow_symlinks`` is on, refuse to read
                # bytes outside the bundle root. A malicious ``.app`` with
                # a symlink to ``~/.ssh/id_rsa`` must not stream those
                # bytes into the bundle hash.
                resolved_fp = resolve_for_check(fp)
                try:
                    resolved_fp.relative_to(resolved_bundle_root)
                except ValueError:
                    log.warning(
                        "Bundle member escapes root; skipping: %s -> %s",
                        fp,
                        resolved_fp,
                    )
                    continue
                stf = fp.stat()
            except OSError:
                continue
            try:
                h = blake3.blake3()
                with fp.open("rb") as f:
                    while True:
                        buf = f.read(1 << 20)
                        if not buf:
                            break
                        h.update(buf)
                digest = str(h.hexdigest())
            except OSError as exc:
                log.debug("Bundle member hash failed for %s: %s", fp, exc)
                continue
            rel = str(fp.relative_to(bundle_path))
            triples.append((rel, int(stf.st_size), digest))
            total_size += int(stf.st_size)
            latest_mtime = max(latest_mtime, float(stf.st_mtime))

    if not triples and total_size == 0:
        # Empty bundle — still emit one record so its "empty" state groups
        # with other empty bundles of the same extension.
        latest_mtime = latest_mtime or float(st_root.st_mtime)

    triples.sort()
    assembly = blake3.blake3()
    for rel, size, digest in triples:
        assembly.update(rel.encode("utf-8"))
        assembly.update(b"\0")
        assembly.update(str(size).encode("ascii"))
        assembly.update(b"\0")
        assembly.update(digest.encode("ascii"))
        assembly.update(b"\n")
    bundle_hash = str(assembly.hexdigest())

    if total_size < min_size_bytes:
        return None

    return FileRecord(
        path=bundle_path,
        size=total_size,
        mtime=latest_mtime or float(st_root.st_mtime),
        inode=int(st_root.st_ino),
        dev=int(st_root.st_dev),
        nlink=int(st_root.st_nlink),
        is_bundle=True,
        precomputed_full_hash=bundle_hash,
    )
