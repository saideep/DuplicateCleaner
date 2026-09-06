"""G4: undo's basename+size fallback must handle collision-renames and
verify the trashed file's actual content against the manifest hash.

macOS ``send2trash`` renames on same-basename collision: an existing
``foo.txt`` in Trash + a new ``foo.txt`` → the new file lands as
``foo 2.txt`` (or ``foo N.txt``). The pre-G4 fallback matched
``candidate.name == name`` exactly and would miss the collision-renamed
file entirely. It also ignored the manifest's ``hash`` field, so two
files with the same basename+size but different content could be picked
apart from each other by luck alone.
"""
from __future__ import annotations

import json
from pathlib import Path

import blake3  # type: ignore[import-untyped]

from duplicate_cleaner.apply.undo import restore_from_manifest


def _blake3(data: bytes) -> str:
    return str(blake3.blake3(data).hexdigest())


def test_undo_picks_collision_renamed_file_by_hash(tmp_path: Path) -> None:
    """Trash contains foo.txt AND foo 2.txt with the same size — only one
    hashes to the manifest hash. Undo must pick that one.
    """
    original = tmp_path / "foo.txt"
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    # A pre-existing "foo.txt" in the Trash from an earlier deletion —
    # its bytes are unrelated to our restore target.
    (fake_trash / "foo.txt").write_bytes(b"OLD_content_xxxxxxxx")
    # Our target landed under a collision-rename.
    target_content = b"NEW_content_yyyyyyyy"
    (fake_trash / "foo 2.txt").write_bytes(target_content)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": len(target_content),
                        "mtime": 1000.0,
                        "hash": _blake3(target_content),
                        "trashed_at_path": None,
                    }
                ],
            }
        )
    )

    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 1, result["errors"]
    assert original.exists()
    assert original.read_bytes() == target_content
    # The old "foo.txt" in the Trash is untouched.
    assert (fake_trash / "foo.txt").exists()


def test_undo_refuses_when_two_candidates_share_basename_and_size_but_differ_from_hash(
    tmp_path: Path,
) -> None:
    """Two files match on (basename, size) but neither content matches the
    manifest hash. Undo must refuse rather than guess."""
    original = tmp_path / "foo.txt"
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    # Two collision-rename siblings, same size, DIFFERENT bytes from
    # each other AND from the manifest hash.
    (fake_trash / "foo.txt").write_bytes(b"A" * 16)
    (fake_trash / "foo 2.txt").write_bytes(b"B" * 16)

    expected_content = b"C" * 16
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": 16,
                        "mtime": 1000.0,
                        "hash": _blake3(expected_content),
                        "trashed_at_path": None,
                    }
                ],
            }
        )
    )

    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 0
    assert not original.exists()
    # An error listing the candidates should be reported.
    assert len(result["errors"]) == 1
    assert "foo" in result["errors"][0]


def test_undo_refuses_ambiguous_hash_match_collision(tmp_path: Path) -> None:
    """Two files with the same basename+size AND identical bytes — hash
    check cannot disambiguate. Undo must refuse and report both.
    """
    original = tmp_path / "foo.txt"
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    content = b"D" * 32
    (fake_trash / "foo.txt").write_bytes(content)
    (fake_trash / "foo 2.txt").write_bytes(content)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": len(content),
                        "mtime": 1000.0,
                        "hash": _blake3(content),
                        "trashed_at_path": None,
                    }
                ],
            }
        )
    )
    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 0
    assert not original.exists()
    assert result["errors"]
    assert "Multiple" in result["errors"][0] or "multiple" in result["errors"][0].lower()


def test_undo_falls_back_when_manifest_has_no_hash(tmp_path: Path) -> None:
    """Legacy manifests without a ``hash`` field still restore by basename+size
    when there is exactly one candidate.
    """
    original = tmp_path / "foo.txt"
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    content = b"pre-G4-manifest-content"
    (fake_trash / "foo.txt").write_bytes(content)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "20260905T000000Z",
                "entries": [
                    {
                        "original_path": str(original),
                        "size": len(content),
                        "mtime": 1000.0,
                        "trashed_at_path": None,
                    }
                ],
            }
        )
    )
    result = restore_from_manifest(
        manifest,
        trash_dir_resolver=lambda _p: fake_trash,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 1, result["errors"]
    assert original.read_bytes() == content
