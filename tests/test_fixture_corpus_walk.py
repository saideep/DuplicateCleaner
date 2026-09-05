"""The fixture corpus is scanned end-to-end by the walker.

Exercises the entire menagerie built by ``tests/fixtures/build.py``:
hardlinks, symlinks (into normal dirs and into ~/Library), iCloud
placeholders, ``.git/objects``, ``node_modules``, backup folders,
numbered-copy filenames. Assertions are structural — each fixture path
must be either scanned or excluded per the safety contract.
"""
from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.scan.walk import iter_files
from tests.fixtures.build import build_corpus


def test_corpus_scan_yields_expected_paths_and_excludes_forbidden(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    # Skip the real git repo — walker excludes ``.git`` so its presence
    # doesn't affect the checked-in files, but avoiding subprocess makes
    # the test hermetic.
    layout = build_corpus(corpus, include_git_repo=False)

    records = list(iter_files([corpus]))
    paths = {r.path.resolve() for r in records}

    # ---- Must appear ----------------------------------------------------
    for included in (
        layout.exact_live,
        layout.exact_backup,
        layout.same_size_a,
        layout.same_size_b,
        layout.head_tail_a,
        layout.head_tail_b,
        layout.head_tail_c_different_tail,
        layout.hardlink_primary,
        layout.hardlink_secondary,
        layout.numbered_copy,
        layout.numbered_copy_original,
        layout.backup_folder_file,
        layout.backup_folder_original,
        layout.symlinked_normal_dir_target / "leaf.txt",
    ):
        assert included.resolve() in paths, f"missing from scan: {included}"

    # ---- Must NOT appear ------------------------------------------------
    for excluded in (
        layout.icloud_placeholder,
        layout.git_object_file,
        layout.node_modules_file,
    ):
        assert excluded.resolve() not in paths, (
            f"excluded path leaked into scan: {excluded}"
        )
    # The library-symlink target must never contribute records — none of
    # the returned paths should have ``~/Library`` as their canonical prefix.
    lib_str = str(Path.home() / "Library")
    for p in paths:
        assert lib_str not in str(p), (
            f"symlinked ~/Library leaked into scan: {p}"
        )


def test_corpus_scan_with_follow_symlinks_still_excludes_library(
    tmp_path: Path,
) -> None:
    """Even with follow_symlinks=True, the resolved-path check must keep
    the ~/Library symlink from resurrecting the excluded tree.
    """
    corpus = tmp_path / "corpus"
    build_corpus(corpus, include_git_repo=False)
    records = list(iter_files([corpus], follow_symlinks=True))
    lib_str = str(Path.home() / "Library")
    for r in records:
        assert lib_str not in str(r.path), (
            f"follow_symlinks=True let ~/Library through: {r.path}"
        )
