"""Archive-recursion tests — every case listed in the v0.1.1 plan.

Covers:
* zip with 3 members all also on disk → whole-archive-delete proposal.
* zip with 3 members, only 1 on disk → NO whole-archive proposal;
  archive members are informational-only.
* corrupt zip → skipped with a report entry.
* password-protected zip → skipped with a report entry.
* nested zip depth 3 → depth cap of 2 stops recursion at the deepest.
"""
from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.compare.archive import (
    ArchiveMemberRecord,
    detect_format,
    find_wholly_duplicated_archives,
    is_archive_path,
    is_virtual_archive_path,
    outer_archive_of,
    scan_archive,
)
from tests.fixtures.build import build_archive_corpus


def test_detect_format_matches_supported_extensions() -> None:
    assert detect_format("a.zip") == "zip"
    assert detect_format("a.tar") == "tar"
    assert detect_format("a.tar.gz") == "tar"
    assert detect_format("a.tgz") == "tar"
    assert detect_format("a.tar.bz2") == "tar"
    assert detect_format("a.tbz2") == "tar"
    assert detect_format("a.tar.xz") == "tar"
    assert detect_format("a.txz") == "tar"
    assert detect_format("a.7z") is None
    assert detect_format("a.rar") is None
    assert detect_format("a.dmg") is None
    assert detect_format("plain.txt") is None


def test_is_virtual_and_outer(tmp_path: Path) -> None:
    assert is_virtual_archive_path("a.zip::b.txt")
    assert not is_virtual_archive_path("a.zip")
    assert outer_archive_of("a.zip::b.zip::c.txt") == "a.zip"
    assert outer_archive_of("a.zip") == "a.zip"


def test_scan_zip_all_members_streamed_and_hashed(tmp_path: Path) -> None:
    layout = build_archive_corpus(tmp_path)
    result = scan_archive(layout.dup_all, max_depth=2)
    names = {m.virtual_path for m in result.members}
    assert f"{layout.dup_all}::a.bin" in names
    assert f"{layout.dup_all}::b.bin" in names
    assert f"{layout.dup_all}::c.bin" in names
    for m in result.members:
        assert isinstance(m, ArchiveMemberRecord)
        assert m.is_archive_member is True
        assert len(m.full_hash) == 64  # BLAKE3 hex
    assert result.skips == []


def test_scan_corrupt_zip_reports_skip(tmp_path: Path) -> None:
    layout = build_archive_corpus(tmp_path)
    result = scan_archive(layout.corrupt, max_depth=2)
    # Either the outer archive fails to open, or a member read fails.
    assert result.skips, "expected at least one skip entry for corrupt zip"
    reasons = {s.reason for s in result.skips}
    assert "corrupt" in reasons


def test_scan_encrypted_zip_reports_skip_without_prompting(tmp_path: Path) -> None:
    layout = build_archive_corpus(tmp_path)
    result = scan_archive(layout.encrypted, max_depth=2)
    reasons = {s.reason for s in result.skips}
    assert "encrypted" in reasons
    # No member yielded — we never trust a decrypted member without a password.
    assert not any(
        m.virtual_path.endswith("secret.bin") for m in result.members
    )


def test_nested_depth_cap_stops_recursion(tmp_path: Path) -> None:
    """A 3-deep nested zip with max_depth=2: the deepest zip is hashed
    opaquely as one member; its own inner ``leaf.bin`` is never yielded."""
    layout = build_archive_corpus(tmp_path)
    result = scan_archive(layout.nested_outer, max_depth=2)
    virtuals = {m.virtual_path for m in result.members}
    # Depth 1: outer.zip::mid.zip is emitted, opaque
    assert f"{layout.nested_outer}::mid.zip" in virtuals
    # Depth 2: mid.zip::inner.zip is emitted, opaque (since max=2)
    # Depth 3 (leaf.bin) MUST NOT be recursed into.
    assert not any(v.endswith("::leaf.bin") for v in virtuals), (
        f"leaf.bin was recursed into at depth 3: {virtuals}"
    )


def test_wholly_duplicated_archives_proposals(tmp_path: Path) -> None:
    """Given the on-disk-hash universe, only dup_all should be proposed."""
    layout = build_archive_corpus(tmp_path)
    scan_all = scan_archive(layout.dup_all, max_depth=2)
    scan_partial = scan_archive(layout.dup_partial, max_depth=2)
    all_hashes = {m.full_hash for m in scan_all.members}
    # Assume on-disk set contains only dup_all's 3 disk copies + shared.bin.
    ondisk = set(all_hashes)  # all 3 dup_all disk copies present
    ondisk.add(next(iter(scan_partial.members)).full_hash)  # only shared.bin

    proposals = find_wholly_duplicated_archives(
        archive_paths=[layout.dup_all, layout.dup_partial],
        member_hashes_by_archive={
            str(layout.dup_all): [m.full_hash for m in scan_all.members],
            str(layout.dup_partial): [m.full_hash for m in scan_partial.members],
        },
        hashes_with_ondisk_copy=ondisk,
    )
    assert proposals == [layout.dup_all]


def test_is_archive_path_matches_extensions(tmp_path: Path) -> None:
    assert is_archive_path(Path("/x/foo.zip"))
    assert is_archive_path(Path("/x/foo.tar.gz"))
    assert not is_archive_path(Path("/x/foo.txt"))


def test_find_wholly_duplicated_ignores_archives_with_skips() -> None:
    """H1: an archive whose walk produced ANY skip must never be proposed
    for whole-archive-delete. Encrypted, corrupt, or nested-too-large
    members leave content unaccounted for — deleting the outer archive
    would silently discard those bytes.
    """
    from duplicate_cleaner.compare.archive import find_wholly_duplicated_archives

    a = Path("/tmp/dc-test/a.zip")
    b = Path("/tmp/dc-test/b.zip")
    proposals = find_wholly_duplicated_archives(
        archive_paths=[a, b],
        member_hashes_by_archive={str(a): ["h1", "h2"], str(b): ["h1", "h2"]},
        hashes_with_ondisk_copy={"h1", "h2"},
        skipped_archives={str(a)},
    )
    # a is skipped even though its readable members are all duplicated on-disk.
    assert proposals == [b]


def test_archive_with_encrypted_member_is_never_whole_deleted(tmp_path: Path) -> None:
    """H1: build an archive with one readable-duplicated member + one
    encrypted member. Confirm no whole-archive proposal.
    """
    import zipfile

    from duplicate_cleaner.compare.archive import find_wholly_duplicated_archives
    from tests.fixtures.build import _flip_encrypted_flag

    # Create archive containing two members.
    zp = tmp_path / "mixed.zip"
    a_bytes = b"A" * 1024
    e_bytes = b"E" * 1024
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("public.bin", a_bytes)
        zf.writestr("secret.bin", e_bytes)
    # Now flip the encryption flag on the "secret.bin" — we can't
    # selectively encrypt with stdlib, so we mark BOTH encrypted. This is
    # enough to demonstrate the skip → no-propose linkage.
    _flip_encrypted_flag(zp)

    result = scan_archive(zp, max_depth=2)
    # Encrypted members produce skips.
    assert result.skips, "expected an encrypted-member skip"
    assert any(s.reason == "encrypted" for s in result.skips)

    # Even if the readable members' hashes are all present on disk, the
    # archive must NOT be proposed for whole-delete because its walk had
    # skips.
    on_disk = {m.full_hash for m in result.members}
    skipped_outer = {str(zp)}  # simulate the cli's per-archive skip index
    proposals = find_wholly_duplicated_archives(
        archive_paths=[zp],
        member_hashes_by_archive={
            str(zp): [m.full_hash for m in result.members]
        },
        hashes_with_ondisk_copy=on_disk,
        skipped_archives=skipped_outer,
    )
    assert proposals == []


def test_archive_with_corrupt_member_is_never_whole_deleted(tmp_path: Path) -> None:
    """H1: use the fixture ``corrupt.zip`` which triggers a corrupt-skip.
    A corrupt archive must NEVER be proposed for whole-delete.
    """
    from duplicate_cleaner.compare.archive import find_wholly_duplicated_archives
    from tests.fixtures.build import build_archive_corpus

    layout = build_archive_corpus(tmp_path)
    result = scan_archive(layout.corrupt, max_depth=2)
    assert any(s.reason == "corrupt" for s in result.skips)

    proposals = find_wholly_duplicated_archives(
        archive_paths=[layout.corrupt],
        member_hashes_by_archive={
            str(layout.corrupt): [m.full_hash for m in result.members]
        },
        # Even if we lied and said every member's hash is on-disk elsewhere,
        # the skipped-archive gate must still veto the proposal.
        hashes_with_ondisk_copy={m.full_hash for m in result.members},
        skipped_archives={str(layout.corrupt)},
    )
    assert proposals == []


def test_nested_archive_exceeding_cap_is_skipped(tmp_path: Path) -> None:
    """H4: an outer archive containing a nested archive whose expanded size
    exceeds ``max_nested_archive_bytes`` must NOT be recursed into. The
    outer walk records a "nested_archive_too_large" skip and does not
    stream 40 GB into RAM.
    """
    import io
    import zipfile

    # Build a nested inner zip that expands to well over our tiny cap.
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("bigmember.bin", b"X" * 8192)
    inner_bytes = inner_buf.getvalue()

    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("nested.zip", inner_bytes)

    # Cap at 1024 bytes — the nested zip is much larger.
    result = scan_archive(outer, max_depth=2, max_nested_archive_bytes=1024)

    # The nested archive is recorded as a skip with the too-large reason.
    reasons = {s.reason for s in result.skips}
    assert "nested_archive_too_large" in reasons

    # And its inner ``bigmember.bin`` was NOT yielded — recursion never
    # happened because the spool overflow returned None.
    virtuals = {m.virtual_path for m in result.members}
    assert not any(v.endswith("::bigmember.bin") for v in virtuals), (
        f"nested archive was recursed into despite cap: {virtuals}"
    )
