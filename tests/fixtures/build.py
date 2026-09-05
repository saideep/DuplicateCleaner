"""Scripted fixture builder — constructs a deterministic corpus of duplicates.

Called from tests with a ``tmp_path``. Everything below is generated with
fixed byte content and fixed mtimes so runs are reproducible. Total footprint
stays under 1 MB.

The corpus is designed to exercise every code path documented in the plan:

* exact duplicates across two directories, one under a "backup" name
* same-size / different-content files (size bucket → partial → full)
* head-identical / tail-different (partial-hash head+tail branch)
* a hard-linked pair (same inode)
* symlinked directories — one to a normal dir, one into ``~/Library`` (which
  must be excluded even with ``follow_symlinks=True``)
* an iCloud placeholder (``*.icloud``)
* a file inside ``.git/objects/`` (must be excluded)
* a file inside ``node_modules/`` (must be excluded)
* a small real git repo (for the "clean git" scoring signal)
* a ``foo (1).txt``-named file (numbered-copy filename signal)
* a file inside a ``backup/`` folder (backup-folder path signal)

All content is small — every fixture file fits in a handful of KB — and the
partial-hash-boundary files are exactly 256 KB, which is the smallest size
that reliably exercises the head+tail branch (``PARTIAL_CHUNK * 4 = 256 KB``).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from duplicate_cleaner.hash.pipeline import PARTIAL_CHUNK

# Deterministic mtimes so scan runs are reproducible.
_MTIME_OLDER = 1_700_000_000.0
_MTIME_NEWER = 1_710_000_000.0


@dataclass(frozen=True)
class FixtureLayout:
    """Absolute paths to every notable file in the corpus.

    Tests reference specific members by name. Keeping the layout in a
    dataclass avoids brittle "count the files" assertions.
    """

    root: Path
    # Exact duplicates.
    exact_live: Path
    exact_backup: Path
    # Same-size, different-content (must not group).
    same_size_a: Path
    same_size_b: Path
    # Head-identical / tail-different (>128 KB — exercises head+tail branch).
    head_tail_a: Path
    head_tail_b: Path
    head_tail_c_different_tail: Path
    # Hard-linked pair (same inode).
    hardlink_primary: Path
    hardlink_secondary: Path
    # Symlink directory into a scannable dir.
    symlinked_normal_dir: Path
    symlinked_normal_dir_target: Path
    # Symlink directory into ~/Library (must be excluded even with follow).
    symlinked_library: Path
    # iCloud placeholder.
    icloud_placeholder: Path
    # Files inside excluded dirs.
    git_object_file: Path
    node_modules_file: Path
    # Tiny mock git repo (working tree clean).
    git_repo_dir: Path
    git_repo_file: Path
    # Numbered-copy filename.
    numbered_copy: Path
    numbered_copy_original: Path
    # File under a "backup/" folder for the folder-name scorer signal.
    backup_folder_file: Path
    backup_folder_original: Path


_EXACT_CONTENT = b"exact-duplicate-body-" * 100  # 2.1 KB
_SAME_SIZE_A = b"AAAA" * 500  # 2000 bytes
_SAME_SIZE_B = b"BBBB" * 500  # 2000 bytes — same size, different bytes
_HEAD = b"H" * PARTIAL_CHUNK
_MIDDLE = b"M" * (PARTIAL_CHUNK * 2)  # 128 KB filler so total > PARTIAL_CHUNK*2
_TAIL_SAME = b"T" * PARTIAL_CHUNK
_TAIL_DIFF = b"X" * PARTIAL_CHUNK
_HARDLINK_CONTENT = b"hard-linked-file-body-" * 50
_NUMBERED_CONTENT = b"numbered-copy-content-" * 60
_BACKUP_FOLDER_CONTENT = b"file-that-lives-in-backup-folder-" * 40
_GIT_REPO_CONTENT = b"tracked-file-content\n"


def _write(path: Path, data: bytes, mtime: float = _MTIME_OLDER) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _init_tiny_git_repo(repo: Path) -> None:
    """Create a real git repo with one committed file and a clean tree.

    Uses env vars so no global user config is required.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        # Silence the default-branch-name hint when older gits are used.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env
    )
    tracked = repo / "tracked.txt"
    tracked.write_bytes(_GIT_REPO_CONTENT)
    subprocess.run(
        ["git", "-C", str(repo), "add", "tracked.txt"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        env=env,
    )


def build_corpus(root: Path, *, include_git_repo: bool = True) -> FixtureLayout:
    """Populate ``root`` with the full fixture corpus and return a layout struct.

    ``include_git_repo`` is opt-out because it requires ``git`` on ``PATH``
    and is slower than the rest of the corpus. Tests that don't need the
    clean-git-tree signal skip it.
    """
    root.mkdir(parents=True, exist_ok=True)

    # --- Exact duplicates: live vs backup folder --------------------------
    exact_live = _write(root / "live" / "notes.txt", _EXACT_CONTENT, _MTIME_NEWER)
    exact_backup = _write(
        root / "OldMac_backup" / "notes.txt", _EXACT_CONTENT, _MTIME_OLDER
    )

    # --- Same size, different bytes ---------------------------------------
    same_size_a = _write(root / "sizes" / "left.bin", _SAME_SIZE_A)
    same_size_b = _write(root / "sizes" / "right.bin", _SAME_SIZE_B)
    assert same_size_a.stat().st_size == same_size_b.stat().st_size

    # --- Head-identical / tail-different (>128 KB) -----------------------
    head_tail_body = _HEAD + _MIDDLE + _TAIL_SAME
    head_tail_a = _write(root / "large" / "a.bin", head_tail_body)
    head_tail_b = _write(root / "large" / "b.bin", head_tail_body)
    head_tail_c = _write(
        root / "large" / "c_different_tail.bin", _HEAD + _MIDDLE + _TAIL_DIFF
    )

    # --- Hard-linked pair -------------------------------------------------
    hardlink_primary = _write(
        root / "linked" / "primary.dat", _HARDLINK_CONTENT
    )
    hardlink_secondary = root / "linked" / "secondary.dat"
    os.link(hardlink_primary, hardlink_secondary)
    assert hardlink_primary.stat().st_ino == hardlink_secondary.stat().st_ino

    # --- Symlink directories ---------------------------------------------
    normal_target = root / "linktargets" / "real_dir"
    normal_target.mkdir(parents=True)
    _write(normal_target / "leaf.txt", b"leaf-file-body", _MTIME_NEWER)
    symlinked_normal_dir = root / "symlinks" / "into_normal"
    symlinked_normal_dir.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(normal_target, symlinked_normal_dir)

    symlinked_library = root / "symlinks" / "into_library"
    os.symlink(Path.home() / "Library", symlinked_library)

    # --- iCloud placeholder ----------------------------------------------
    icloud_placeholder = _write(root / "cloud" / ".doc.icloud", b"")

    # --- Excluded-dir contents -------------------------------------------
    git_object_file = _write(
        root / "some_repo" / ".git" / "objects" / "ab" / "cd", b"gitobjbytes"
    )
    node_modules_file = _write(
        root / "some_project" / "node_modules" / "pkg" / "index.js",
        b"module.exports = {};\n",
    )

    # --- Tiny git repo ---------------------------------------------------
    git_repo_dir = root / "clean_repo"
    git_repo_file = git_repo_dir / "tracked.txt"
    if include_git_repo:
        _init_tiny_git_repo(git_repo_dir)
    else:
        _write(git_repo_file, _GIT_REPO_CONTENT)

    # --- Numbered-copy filename ------------------------------------------
    numbered_copy_original = _write(
        root / "downloads" / "foo.txt", _NUMBERED_CONTENT, _MTIME_NEWER
    )
    numbered_copy = _write(
        root / "downloads" / "foo (1).txt", _NUMBERED_CONTENT, _MTIME_OLDER
    )

    # --- Backup-folder file ----------------------------------------------
    backup_folder_original = _write(
        root / "docs" / "report.txt", _BACKUP_FOLDER_CONTENT, _MTIME_NEWER
    )
    backup_folder_file = _write(
        root / "docs" / "backup" / "report.txt",
        _BACKUP_FOLDER_CONTENT,
        _MTIME_OLDER,
    )

    return FixtureLayout(
        root=root,
        exact_live=exact_live,
        exact_backup=exact_backup,
        same_size_a=same_size_a,
        same_size_b=same_size_b,
        head_tail_a=head_tail_a,
        head_tail_b=head_tail_b,
        head_tail_c_different_tail=head_tail_c,
        hardlink_primary=hardlink_primary,
        hardlink_secondary=hardlink_secondary,
        symlinked_normal_dir=symlinked_normal_dir,
        symlinked_normal_dir_target=normal_target,
        symlinked_library=symlinked_library,
        icloud_placeholder=icloud_placeholder,
        git_object_file=git_object_file,
        node_modules_file=node_modules_file,
        git_repo_dir=git_repo_dir,
        git_repo_file=git_repo_file,
        numbered_copy=numbered_copy,
        numbered_copy_original=numbered_copy_original,
        backup_folder_file=backup_folder_file,
        backup_folder_original=backup_folder_original,
    )


# ---------------------------------------------------------------------------
# v0.1.1 fixtures: archives + bundles. Kept separate from ``build_corpus`` so
# individual tests only pay for what they touch.
# ---------------------------------------------------------------------------

import zipfile  # noqa: E402


@dataclass(frozen=True)
class ArchiveLayout:
    """Files and archives laid down for archive-recursion tests."""

    root: Path
    dup_all: Path  # zip whose every member is duplicated on-disk
    dup_all_disk_members: list[Path]
    dup_partial: Path  # zip where only some members are duplicated
    dup_partial_disk_member: Path
    corrupt: Path
    encrypted: Path
    nested_outer: Path  # zip containing a zip containing a zip
    nested_deepest_member_content: bytes


def _write_zip(
    zip_path: Path,
    entries: list[tuple[str, bytes]],
    *,
    encrypted: bool = False,
) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    if encrypted:
        _flip_encrypted_flag(zip_path)


def _flip_encrypted_flag(zip_path: Path) -> None:
    """Patch a plain-text zip to look encrypted at the wire level.

    Sets bit 0 of the general-purpose bit flag on every local file header
    (LFH, magic ``PK\\x03\\x04``) and every central directory entry
    (magic ``PK\\x01\\x02``). Python's ``zipfile`` won't decrypt without
    a password, so the reader raises ``RuntimeError`` — which our scan
    treats as ``reason="encrypted"``.

    Producing a real DES-encrypted zip from stdlib alone is not possible;
    this is the minimum change that makes ``zipfile`` believe the archive
    is encrypted, which is all our scan_archive path checks.
    """
    data = bytearray(zip_path.read_bytes())
    lfh = b"PK\x03\x04"
    cd = b"PK\x01\x02"
    i = 0
    while i < len(data) - 4:
        if data[i : i + 4] == lfh:
            # Flag word at offset 6 from the LFH signature.
            data[i + 6] |= 0x01
            i += 30
        elif data[i : i + 4] == cd:
            # Flag word at offset 8 from the CD signature.
            data[i + 8] |= 0x01
            i += 46
        else:
            i += 1
    zip_path.write_bytes(bytes(data))


def build_archive_corpus(root: Path) -> ArchiveLayout:
    """Populate ``root`` with archives that exercise every plan requirement.

    * ``dup_all.zip`` — 3 members, each also present as a real on-disk file.
      Whole-archive-delete proposal expected.
    * ``dup_partial.zip`` — 3 members, only 1 is duplicated on-disk. No
      whole-archive proposal; contents remain informational.
    * ``corrupt.zip`` — truncated headers.
    * ``encrypted.zip`` — password-protected members.
    * ``nested_outer.zip`` — depth 3: zip inside zip inside zip. Depth cap
      of 2 means the deepest zip is opaque, not recursed into.
    """
    root.mkdir(parents=True, exist_ok=True)

    # -- dup_all: 3 members, every one also present at root as a real file --
    disk_dir = root / "disk"
    disk_dir.mkdir()
    a_bytes = b"A" * 1024
    b_bytes = b"B" * 1024
    c_bytes = b"C" * 1024
    for name, data in (("a.bin", a_bytes), ("b.bin", b_bytes), ("c.bin", c_bytes)):
        _write(disk_dir / name, data)
    dup_all = root / "dup_all.zip"
    _write_zip(
        dup_all,
        [("a.bin", a_bytes), ("b.bin", b_bytes), ("c.bin", c_bytes)],
    )

    # -- dup_partial: only one member also on disk --
    partial_disk = _write(root / "disk" / "shared.bin", b"S" * 1024)
    dup_partial = root / "dup_partial.zip"
    _write_zip(
        dup_partial,
        [
            ("shared.bin", b"S" * 1024),
            ("only_in_zip_1.bin", b"X" * 1024),
            ("only_in_zip_2.bin", b"Y" * 1024),
        ],
    )

    # -- corrupt: a real zip then truncate --
    corrupt = root / "corrupt.zip"
    _write_zip(corrupt, [("junk.bin", b"junk" * 300)])
    with open(corrupt, "r+b") as f:
        f.truncate(48)  # decapitate the central directory

    # -- encrypted: password-protected member --
    encrypted = root / "encrypted.zip"
    _write_zip(
        encrypted, [("secret.bin", b"secret" * 300)], encrypted=True
    )

    # -- nested depth 3: outer.zip -> mid.zip -> inner.zip -> leaf.bin --
    deepest_content = b"DEEP" * 512
    import io

    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("leaf.bin", deepest_content)
    inner_bytes = inner_buf.getvalue()

    mid_buf = io.BytesIO()
    with zipfile.ZipFile(mid_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inner.zip", inner_bytes)
    mid_bytes = mid_buf.getvalue()

    nested_outer = root / "nested_outer.zip"
    with zipfile.ZipFile(nested_outer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mid.zip", mid_bytes)

    return ArchiveLayout(
        root=root,
        dup_all=dup_all,
        dup_all_disk_members=[disk_dir / "a.bin", disk_dir / "b.bin", disk_dir / "c.bin"],
        dup_partial=dup_partial,
        dup_partial_disk_member=partial_disk,
        corrupt=corrupt,
        encrypted=encrypted,
        nested_outer=nested_outer,
        nested_deepest_member_content=deepest_content,
    )


@dataclass(frozen=True)
class BundleLayout:
    """A ``.app`` bundle with a handful of files inside."""

    root: Path
    app_dir: Path
    inner_files: list[Path]


def build_bundle_corpus(root: Path) -> BundleLayout:
    """Create a fake ``.app`` bundle with 3 files inside.

    The walker must yield ONE record for the whole ``.app`` — not one per
    file inside.
    """
    root.mkdir(parents=True, exist_ok=True)
    app_dir = root / "Fake.app"
    contents = app_dir / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    files = [
        _write(contents / "Info.plist", b"<?xml version='1.0'?>", _MTIME_OLDER),
        _write(macos / "Fake", b"binary-body-bytes" * 100, _MTIME_OLDER),
        _write(resources / "icon.icns", b"icon-data" * 100, _MTIME_OLDER),
    ]
    return BundleLayout(root=root, app_dir=app_dir, inner_files=files)
