"""Bundle handling: the walker yields ONE record for a whole ``.app``.

From the plan: bundles are directories macOS presents as opaque units. The
walker must NOT descend into them; it must hash the entire tree as a single
unit and yield exactly one FileRecord per bundle.
"""
from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.config import DEFAULT_BUNDLE_EXTENSIONS
from duplicate_cleaner.scan.walk import iter_files
from tests.fixtures.build import build_bundle_corpus


def test_bundle_yields_one_record(tmp_path: Path) -> None:
    layout = build_bundle_corpus(tmp_path)
    records = list(
        iter_files(
            [layout.root],
            bundle_extensions=DEFAULT_BUNDLE_EXTENSIONS,
        )
    )
    # Exactly one record, and it points at the .app directory itself.
    app_records = [r for r in records if r.path.name.endswith(".app")]
    assert len(app_records) == 1
    rec = app_records[0]
    assert rec.is_bundle is True
    assert rec.precomputed_full_hash is not None
    assert len(rec.precomputed_full_hash) == 64
    # No records for individual inner files should be present.
    for inner in layout.inner_files:
        assert not any(r.path == inner for r in records), (
            f"walker descended into bundle for {inner}"
        )


def test_bundle_disabled_when_no_extensions(tmp_path: Path) -> None:
    """Without bundle_extensions, the walker treats the directory as normal
    and yields records for every file inside — the pre-v0.1.1 behavior."""
    layout = build_bundle_corpus(tmp_path)
    records = list(iter_files([layout.root]))
    inner_paths = {r.path for r in records}
    for inner in layout.inner_files:
        # Paths from iter_files are unresolved — walker sees exactly what we
        # created since no symlinks are in play.
        assert inner in inner_paths or inner.resolve() in inner_paths


def test_bundle_symlink_escape_is_skipped(tmp_path: Path) -> None:
    """H8: a bundle containing a symlink pointing outside the bundle root
    must NOT have the target file's bytes read into the bundle hash. The
    escaping symlink is skipped with a warning.
    """
    from duplicate_cleaner.scan.walk import _hash_bundle

    # File outside the bundle — should never be opened.
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir()
    outside_bytes = b"OUTSIDE-BUNDLE-SECRET-DATA-XXX"
    outside.write_bytes(outside_bytes)

    # Build a bundle with an escaping symlink AND a legitimate normal file.
    app_dir = tmp_path / "Malicious.app"
    (app_dir / "Contents").mkdir(parents=True)
    normal_body = b"normal-body"
    (app_dir / "Contents" / "Info.plist").write_bytes(normal_body)
    (app_dir / "Contents" / "escape_link").symlink_to(outside)

    # Hash with follow_symlinks=True — this is the vulnerable path.
    rec = _hash_bundle(app_dir, min_size_bytes=0, follow_symlinks=True)
    assert rec is not None

    # If the escaping symlink was NOT skipped, the bundle hash would depend
    # on ``outside_bytes``. We verify skipping by re-hashing a control
    # bundle that has the SAME legit member but no symlink at all — the
    # two bundle hashes must match, which they can only do if the escape
    # was ignored.
    control_dir = tmp_path / "Control.app"
    (control_dir / "Contents").mkdir(parents=True)
    (control_dir / "Contents" / "Info.plist").write_bytes(normal_body)
    control_rec = _hash_bundle(control_dir, min_size_bytes=0, follow_symlinks=True)
    assert control_rec is not None
    assert rec.precomputed_full_hash == control_rec.precomputed_full_hash

    # And the outside file's bytes are still intact — nothing wrote to it.
    assert outside.read_bytes() == outside_bytes


def test_identical_bundles_produce_matching_hashes(tmp_path: Path) -> None:
    """Two identical .app trees yield the same bundle hash — the mechanism
    that lets duplicate-detection catch bundle-level duplicates."""
    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    a = build_bundle_corpus(a_root)
    b = build_bundle_corpus(b_root)
    a_recs = list(
        iter_files([a.root], bundle_extensions=DEFAULT_BUNDLE_EXTENSIONS)
    )
    b_recs = list(
        iter_files([b.root], bundle_extensions=DEFAULT_BUNDLE_EXTENSIONS)
    )
    a_hash = next(r.precomputed_full_hash for r in a_recs if r.is_bundle)
    b_hash = next(r.precomputed_full_hash for r in b_recs if r.is_bundle)
    assert a_hash == b_hash
