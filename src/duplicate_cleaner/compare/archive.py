"""Recurse into archives — emit virtual FileRecords for their members.

Stdlib-only (``zipfile`` + ``tarfile``); no third-party archive parsers.
Streaming: bytes go straight into BLAKE3, never to disk.

Virtual paths use ``outer.zip::inner/file.txt`` (``::`` separator). The
``is_archive_member`` flag on emitted records lets ``apply/mover.py`` reject
any single-member discard proposal — the mover MUST propose only the whole
outer archive when every member is a duplicate somewhere else.
"""
from __future__ import annotations

import logging
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import blake3  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

ARCHIVE_SEP = "::"

# 1 MiB read blocks — same as the file hashing pipeline; bytes stream in and
# out without persisting on disk.
_READ_BLOCK = 1 << 20

# H4: cap on nested-archive expansion. A 40 GB decompressed inner archive
# would otherwise land entirely in RAM via ``reader.read()``. Nested
# archives are streamed into a SpooledTemporaryFile with this cap; if the
# spool rolls over to disk we treat the nested archive as too large,
# record a skip, and do not descend.
DEFAULT_MAX_NESTED_ARCHIVE_BYTES = 512 * (1 << 20)  # 512 MiB

# Extension → format tag. ``.tgz`` and ``.tbz2`` and ``.txz`` are aliases
# for the compressed tar variants and share the same reader.
_ZIP_EXTS: frozenset[str] = frozenset({".zip"})
_TAR_EXTS: frozenset[str] = frozenset(
    {
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
    }
)


@dataclass(frozen=True)
class ArchiveMemberRecord:
    """Streaming hash of one member inside an archive.

    ``virtual_path`` is ``outer_archive::relative/inside/archive``. Nested
    archives concatenate — depth-2 members look like ``a.zip::b.zip::c.txt``.
    """

    virtual_path: str
    size: int
    full_hash: str
    is_archive_member: bool = True


@dataclass(frozen=True)
class ArchiveSkip:
    """An archive that could not be walked — corrupt, encrypted, unsupported."""

    path: str
    reason: str
    error: str | None = None


@dataclass
class ArchiveScanResult:
    """Everything one archive walk produced — members + skips."""

    members: list[ArchiveMemberRecord]
    skips: list[ArchiveSkip]


def detect_format(name: str) -> str | None:
    """Return ``"zip"`` / ``"tar"`` for supported names, ``None`` otherwise.

    Match on lower-cased suffix. Longest suffix wins so ``.tar.gz`` binds
    before ``.gz`` would (we don't support standalone ``.gz`` — a single
    compressed file is not a container).
    """
    lower = name.lower()
    for ext in _TAR_EXTS:
        if lower.endswith(ext):
            return "tar"
    for ext in _ZIP_EXTS:
        if lower.endswith(ext):
            return "zip"
    return None


def is_archive_path(path: Path) -> bool:
    """True if the on-disk name is a supported archive extension."""
    return detect_format(path.name) is not None


def _hash_stream(reader: IO[bytes], chunk: int = _READ_BLOCK) -> tuple[str, int]:
    """BLAKE3 hex + byte-count of everything ``reader.read`` returns.

    ``reader`` is any object with a ``read(n) -> bytes`` method. Both
    ``zipfile.ZipExtFile`` and the file object returned by
    ``TarFile.extractfile`` satisfy this.
    """
    h = blake3.blake3()
    total = 0
    while True:
        buf = reader.read(chunk)
        if not buf:
            break
        h.update(buf)
        total += len(buf)
    return str(h.hexdigest()), total


def _walk_zip(
    zpath: Path,
    prefix: str,
    depth: int,
    max_depth: int,
    result: ArchiveScanResult,
    max_nested_bytes: int,
) -> None:
    """Enumerate ``zpath``; recurse into nested archives up to ``max_depth``."""
    try:
        zf = zipfile.ZipFile(str(zpath), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        result.skips.append(
            ArchiveSkip(path=prefix, reason="corrupt", error=str(exc))
        )
        return

    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Encrypted entries have bit 0 of the flag word set. zipfile can
            # read encrypted headers but cannot decrypt without a password.
            if info.flag_bits & 0x1:
                result.skips.append(
                    ArchiveSkip(
                        path=f"{prefix}{ARCHIVE_SEP}{info.filename}",
                        reason="encrypted",
                    )
                )
                continue
            member_virtual = f"{prefix}{ARCHIVE_SEP}{info.filename}"
            try:
                with zf.open(info, "r") as reader:
                    _handle_member(
                        member_virtual,
                        info.filename,
                        reader,
                        depth,
                        max_depth,
                        result,
                        max_nested_bytes,
                    )
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                result.skips.append(
                    ArchiveSkip(
                        path=member_virtual,
                        reason="corrupt",
                        error=str(exc),
                    )
                )


def _walk_tar(
    tpath: Path,
    prefix: str,
    depth: int,
    max_depth: int,
    result: ArchiveScanResult,
    max_nested_bytes: int,
) -> None:
    """Enumerate ``tpath``; recurse into nested archives up to ``max_depth``."""
    try:
        tf = tarfile.open(str(tpath), "r:*")  # noqa: SIM115  # closed via `with tf:` below
    except (tarfile.TarError, OSError) as exc:
        result.skips.append(
            ArchiveSkip(path=prefix, reason="corrupt", error=str(exc))
        )
        return

    with tf:
        for member in tf:
            if not member.isfile():
                continue
            member_virtual = f"{prefix}{ARCHIVE_SEP}{member.name}"
            try:
                reader = tf.extractfile(member)
            except (tarfile.TarError, OSError) as exc:
                result.skips.append(
                    ArchiveSkip(
                        path=member_virtual,
                        reason="corrupt",
                        error=str(exc),
                    )
                )
                continue
            if reader is None:
                continue
            try:
                _handle_member(
                    member_virtual,
                    member.name,
                    reader,
                    depth,
                    max_depth,
                    result,
                    max_nested_bytes,
                )
            except (tarfile.TarError, OSError) as exc:
                result.skips.append(
                    ArchiveSkip(
                        path=member_virtual,
                        reason="corrupt",
                        error=str(exc),
                    )
                )
            finally:
                reader.close()


def _spool_member(
    reader: IO[bytes], max_bytes: int
) -> tempfile.SpooledTemporaryFile[bytes] | None:
    """Stream ``reader`` into a memory-capped spool. Returns None if too large.

    H4: prevents ``blob = reader.read()`` from ballooning a 40 GB nested
    archive into 40 GB of RAM. Every read is bounded to ``_READ_BLOCK`` and
    the running total is checked against ``max_bytes`` before appending —
    if the reader is larger than the cap, we close the spool and return
    ``None`` so the caller records a "nested_archive_too_large" skip.
    """
    spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(  # noqa: SIM115
        max_size=max_bytes, mode="w+b"
    )
    total = 0
    try:
        while True:
            buf = reader.read(_READ_BLOCK)
            if not buf:
                break
            total += len(buf)
            if total > max_bytes:
                spool.close()
                return None
            spool.write(buf)
    except Exception:
        spool.close()
        raise
    spool.seek(0)
    return spool


def _handle_member(
    virtual: str,
    inner_name: str,
    reader: IO[bytes],
    depth: int,
    max_depth: int,
    result: ArchiveScanResult,
    max_nested_bytes: int,
) -> None:
    """Hash the member; if it's itself a supported archive within depth, recurse.

    H4: nested-archive recursion streams the member bytes into a
    :class:`tempfile.SpooledTemporaryFile` capped at ``max_nested_bytes``
    (default :data:`DEFAULT_MAX_NESTED_ARCHIVE_BYTES`). If the member is
    larger than the cap it is skipped with reason
    ``"nested_archive_too_large"`` and never descended into — a hostile 40 GB
    inner archive cannot pull 40 GB of RAM through ``reader.read()`` any
    longer. Members that are themselves archives beyond ``max_depth`` are
    hashed opaquely once depth is exhausted (unchanged).
    """
    fmt = detect_format(inner_name)
    if fmt is not None and depth < max_depth:
        spool = _spool_member(reader, max_nested_bytes)
        if spool is None:
            # H4: nested archive exceeds the cap — record a skip against the
            # virtual path and do NOT recurse. Whole-archive-delete callers
            # keyed on ``result.skips`` will refuse any outer archive whose
            # tree contains this skip.
            result.skips.append(
                ArchiveSkip(
                    path=virtual,
                    reason="nested_archive_too_large",
                    error=(
                        f"nested archive exceeds "
                        f"max_nested_archive_bytes={max_nested_bytes}"
                    ),
                )
            )
            return
        try:
            # Hash the nested archive's bytes with a second BLAKE3 pass over
            # the spool — cheap because the spool is typically in memory,
            # and correct even when the spool rolled over (it never does
            # given the cap check above).
            h = blake3.blake3()
            total = 0
            while True:
                buf = spool.read(_READ_BLOCK)
                if not buf:
                    break
                h.update(buf)
                total += len(buf)
            result.members.append(
                ArchiveMemberRecord(
                    virtual_path=virtual,
                    size=total,
                    full_hash=str(h.hexdigest()),
                )
            )
            spool.seek(0)
            if fmt == "zip":
                _walk_zip_fileobj(
                    spool, virtual, depth + 1, max_depth, result, max_nested_bytes
                )
            else:
                _walk_tar_fileobj(
                    spool, virtual, depth + 1, max_depth, result, max_nested_bytes
                )
        finally:
            spool.close()
        return

    digest, total = _hash_stream(reader)
    result.members.append(
        ArchiveMemberRecord(
            virtual_path=virtual,
            size=total,
            full_hash=digest,
        )
    )


def _walk_zip_fileobj(
    stream: IO[bytes],
    prefix: str,
    depth: int,
    max_depth: int,
    result: ArchiveScanResult,
    max_nested_bytes: int,
) -> None:
    try:
        zf = zipfile.ZipFile(stream, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        result.skips.append(
            ArchiveSkip(path=prefix, reason="corrupt", error=str(exc))
        )
        return
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                result.skips.append(
                    ArchiveSkip(
                        path=f"{prefix}{ARCHIVE_SEP}{info.filename}",
                        reason="encrypted",
                    )
                )
                continue
            member_virtual = f"{prefix}{ARCHIVE_SEP}{info.filename}"
            try:
                with zf.open(info, "r") as reader:
                    _handle_member(
                        member_virtual,
                        info.filename,
                        reader,
                        depth,
                        max_depth,
                        result,
                        max_nested_bytes,
                    )
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                result.skips.append(
                    ArchiveSkip(
                        path=member_virtual,
                        reason="corrupt",
                        error=str(exc),
                    )
                )


def _walk_tar_fileobj(
    stream: IO[bytes],
    prefix: str,
    depth: int,
    max_depth: int,
    result: ArchiveScanResult,
    max_nested_bytes: int,
) -> None:
    try:
        tf = tarfile.open(fileobj=stream, mode="r:*")  # noqa: SIM115
    except (tarfile.TarError, OSError) as exc:
        result.skips.append(
            ArchiveSkip(path=prefix, reason="corrupt", error=str(exc))
        )
        return
    with tf:
        for member in tf:
            if not member.isfile():
                continue
            member_virtual = f"{prefix}{ARCHIVE_SEP}{member.name}"
            reader = tf.extractfile(member)
            if reader is None:
                continue
            try:
                _handle_member(
                    member_virtual,
                    member.name,
                    reader,
                    depth,
                    max_depth,
                    result,
                    max_nested_bytes,
                )
            except (tarfile.TarError, OSError) as exc:
                result.skips.append(
                    ArchiveSkip(
                        path=member_virtual, reason="corrupt", error=str(exc)
                    )
                )
            finally:
                reader.close()


def scan_archive(
    archive_path: Path,
    max_depth: int,
    *,
    max_nested_archive_bytes: int = DEFAULT_MAX_NESTED_ARCHIVE_BYTES,
) -> ArchiveScanResult:
    """Walk one archive on disk, returning every member's virtual record.

    ``max_depth`` is inclusive — depth 1 walks only the outer archive; depth
    2 walks one level of nested archives, and so on. Archives at depth >
    ``max_depth`` are hashed opaquely (as one member) but not descended
    into.

    ``max_nested_archive_bytes`` caps the RAM/spool budget for a single
    nested archive during depth-1..``max_depth`` recursion (H4). Nested
    archives larger than the cap are skipped, not descended into.
    """
    result = ArchiveScanResult(members=[], skips=[])
    fmt = detect_format(archive_path.name)
    if fmt is None:
        return result
    prefix = str(archive_path)
    if fmt == "zip":
        _walk_zip(
            archive_path,
            prefix,
            depth=1,
            max_depth=max_depth,
            result=result,
            max_nested_bytes=max_nested_archive_bytes,
        )
    else:
        _walk_tar(
            archive_path,
            prefix,
            depth=1,
            max_depth=max_depth,
            result=result,
            max_nested_bytes=max_nested_archive_bytes,
        )
    return result


def is_virtual_archive_path(path: str) -> bool:
    """True if ``path`` includes the ``::`` archive-member separator."""
    return ARCHIVE_SEP in path


def outer_archive_of(virtual_path: str) -> str:
    """Return the on-disk archive path from a virtual member path.

    ``"a.zip::b.zip::c.txt"`` -> ``"a.zip"``. The mover uses this to decide
    which real file to trash when every member of an outer archive is a
    duplicate elsewhere.
    """
    if ARCHIVE_SEP not in virtual_path:
        return virtual_path
    return virtual_path.split(ARCHIVE_SEP, 1)[0]


def find_wholly_duplicated_archives(
    archive_paths: list[Path],
    member_hashes_by_archive: dict[str, list[str]],
    hashes_with_ondisk_copy: set[str],
    skipped_archives: set[str] | None = None,
) -> list[Path]:
    """Return archives whose every member's hash appears on-disk elsewhere.

    ``member_hashes_by_archive`` maps each on-disk archive path (str) to
    the list of BLAKE3 hashes of its members. ``hashes_with_ondisk_copy``
    is the set of hashes that also appear as a non-archive-member file
    somewhere in the scan — an archive whose every member is in that set
    is redundant on the filesystem.

    ``skipped_archives`` is the set of on-disk archive paths (str) whose
    walk produced at least one skip (encrypted, corrupt, nested archive
    too large). H1: any archive with even one skip along its member tree
    MUST NOT be proposed for whole-delete — the un-hashable content is
    unaccounted for, and deleting the outer archive would silently discard
    it. The archive stays informational only.

    An archive with zero members is never proposed for whole-delete — an
    empty archive is uncorrelated with the rest of the corpus, and
    proposing a delete without evidence would violate the "no silent
    delete" contract.
    """
    skipped = skipped_archives or set()
    proposals: list[Path] = []
    for archive in archive_paths:
        key = str(archive)
        if key in skipped:
            # H1: this archive's walk produced at least one skip. Its
            # readable-member coverage is incomplete — do not propose it.
            continue
        member_hashes = member_hashes_by_archive.get(key)
        if not member_hashes:
            continue
        if all(h in hashes_with_ondisk_copy for h in member_hashes):
            proposals.append(archive)
    return proposals
