"""v0.2 sub-milestone 1 — LocalFileSystemSource wraps iter_files without drift."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from duplicate_cleaner.scan.walk import FileRecord, iter_files
from duplicate_cleaner.sources import (
    LocalFileSystemSource,
    Source,
    SourceMetadata,
    TrashedLocation,
)


def _touch(p: Path, content: bytes = b"x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def test_local_source_list_files_matches_iter_files(tmp_path: Path) -> None:
    _touch(tmp_path / "a.txt", b"alpha")
    _touch(tmp_path / "sub" / "b.txt", b"bravo")
    _touch(tmp_path / "sub" / "deep" / "c.bin", b"charlie-bytes")

    reference = {r.path for r in iter_files([tmp_path])}
    source = LocalFileSystemSource(roots=[tmp_path])
    got = {r.path for r in source.list_files()}
    assert got == reference
    assert got  # sanity: the walker did produce records


def test_local_source_preserves_walker_options(tmp_path: Path) -> None:
    _touch(tmp_path / "small.txt", b"x")
    _touch(tmp_path / "big.txt", b"x" * 20)

    ref = {r.path for r in iter_files([tmp_path], min_size_bytes=5)}
    got = {
        r.path
        for r in LocalFileSystemSource(
            roots=[tmp_path], min_size_bytes=5
        ).list_files()
    }
    assert got == ref
    assert (tmp_path.resolve() / "small.txt") not in got


def test_local_source_is_a_source_protocol_instance(tmp_path: Path) -> None:
    src = LocalFileSystemSource(roots=[tmp_path])
    assert isinstance(src, Source)
    assert src.id == "local"


def test_local_source_read_bytes_streams(tmp_path: Path) -> None:
    payload = b"0123456789" * 1000  # 10 KiB
    f = tmp_path / "blob.bin"
    _touch(f, payload)
    src = LocalFileSystemSource(roots=[tmp_path])
    rec = FileRecord(
        path=f,
        size=len(payload),
        mtime=f.stat().st_mtime,
        inode=f.stat().st_ino,
        dev=f.stat().st_dev,
        nlink=f.stat().st_nlink,
    )
    chunks = list(src.read_bytes(rec, chunk_size=1024))
    assert b"".join(chunks) == payload
    assert len(chunks) == 10  # 10 KiB / 1 KiB chunks


def test_local_source_get_metadata_is_empty(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _touch(f, b"x")
    src = LocalFileSystemSource(roots=[tmp_path])
    rec = next(iter(src.list_files()))
    meta = src.get_metadata(rec)
    assert meta == SourceMetadata()


def test_local_source_move_to_trash_refuses_when_read_only(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _touch(f, b"x")
    src = LocalFileSystemSource(roots=[tmp_path])
    assert src.is_read_only_scan is True
    rec = next(iter(src.list_files()))
    with pytest.raises(PermissionError):
        src.move_to_trash(rec)
    assert f.exists()  # tripwire fired before any filesystem effect


def test_local_source_move_to_trash_uses_injected_trash_fn(
    tmp_path: Path,
) -> None:
    f = tmp_path / "a.txt"
    _touch(f, b"x")

    calls: list[Path] = []

    def fake_trash(p: Path) -> Path | None:
        calls.append(p)
        return Path("/tmp/fake-trash") / p.name

    src = LocalFileSystemSource(
        roots=[tmp_path],
        is_read_only_scan=False,
        trash_fn=fake_trash,
    )
    rec = next(iter(src.list_files()))
    loc = src.move_to_trash(rec)
    assert calls == [rec.path]
    assert f.exists()  # fake didn't actually delete
    assert isinstance(loc, TrashedLocation)
    assert loc.source_id == "local"
    assert loc.original_path == str(rec.path)
    assert loc.cloud_file_id is None
    assert loc.local_trashed_at_path == Path("/tmp/fake-trash") / "a.txt"


def test_local_source_restore_from_trash_roundtrip(tmp_path: Path) -> None:
    """A file dropped in a mock trash dir returns to its original path."""
    original = tmp_path / "docs" / "report.txt"
    _touch(original, b"payload")
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    # Simulate the trash step: move file into fake_trash dir.
    trashed = fake_trash / "report.txt"
    original.rename(trashed)
    assert not original.exists()
    assert trashed.exists()

    src = LocalFileSystemSource(
        roots=[tmp_path],
        is_read_only_scan=False,
        allowed_trash_dirs=[fake_trash],
    )
    loc = TrashedLocation(
        source_id="local",
        original_path=str(original),
        local_trashed_at_path=trashed,
    )
    src.restore_from_trash(loc)
    assert original.exists()
    assert original.read_bytes() == b"payload"
    assert not trashed.exists()


def test_local_source_restore_rejects_non_trash_source(tmp_path: Path) -> None:
    """B9: a poisoned TrashedLocation must not shutil.move an arbitrary file."""
    from duplicate_cleaner.apply.undo import UndoError

    # Point ``trashed_at`` at a fake but not-inside-Trash location.
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    poisoned_src = tmp_path / "outside_trash.txt"
    _touch(poisoned_src, b"secret")
    dst = tmp_path / "docs" / "attacker_target.txt"

    src = LocalFileSystemSource(
        roots=[tmp_path],
        is_read_only_scan=False,
        allowed_trash_dirs=[fake_trash],
    )
    loc = TrashedLocation(
        source_id="local",
        original_path=str(dst),
        local_trashed_at_path=poisoned_src,
    )
    with pytest.raises(UndoError):
        src.restore_from_trash(loc)
    # And the poisoned source is still on disk — no shutil.move happened.
    assert poisoned_src.exists()
    assert not dst.exists()


def test_local_source_restore_rejects_archive_member_original(tmp_path: Path) -> None:
    """B9: ``::`` in original_path must be rejected before any move."""
    from duplicate_cleaner.apply.undo import UndoError

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    trashed = fake_trash / "inner.txt"
    _touch(trashed, b"x")
    src = LocalFileSystemSource(
        roots=[tmp_path],
        is_read_only_scan=False,
        allowed_trash_dirs=[fake_trash],
    )
    loc = TrashedLocation(
        source_id="local",
        original_path=str(tmp_path / "outer.zip::inner.txt"),
        local_trashed_at_path=trashed,
    )
    with pytest.raises(UndoError):
        src.restore_from_trash(loc)
    assert trashed.exists()  # nothing moved


def test_local_source_restore_missing_source_raises(tmp_path: Path) -> None:
    src = LocalFileSystemSource(
        roots=[tmp_path], is_read_only_scan=False
    )
    loc = TrashedLocation(
        source_id="local",
        original_path=str(tmp_path / "gone.txt"),
        local_trashed_at_path=tmp_path / "never-existed.txt",
    )
    with pytest.raises(FileNotFoundError):
        src.restore_from_trash(loc)


def test_file_record_defaults_are_local() -> None:
    rec = FileRecord(
        path=Path("/tmp/x"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
    )
    assert rec.source_id == "local"
    assert rec.foreign_hash is None
    assert rec.etag is None
    assert rec.cloud_file_id is None
    assert rec.owner is None
    assert rec.is_shared is False
    # frozen dataclass — construction site sanity check.
    other = replace(rec, source_id="gdrive:personal", foreign_hash="md5:abc")
    assert other.source_id == "gdrive:personal"
    assert other.foreign_hash == "md5:abc"
