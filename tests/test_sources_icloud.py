"""iCloudPhotosSource — v0.6 local Photos.photoslibrary bundle reader.

Every test injects a fake ``osxphotos`` module so no real Photos database
is opened.  The bundle-existence check is stubbed by pointing the source
at a tmp_path directory instead of ~/Pictures.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.sources.base import (
    SourceDriftError,
    SourceError,
)
from duplicate_cleaner.sources.iclouddrive_photos import iCloudPhotosSource


class _FakePhoto:
    """Minimal osxphotos.PhotoInfo-shaped object for tests."""

    def __init__(
        self,
        *,
        uuid: str,
        path: str | None,
        original_filename: str = "IMG.HEIC",
        date: datetime | None = None,
        date_modified: datetime | None = None,
    ) -> None:
        self.uuid = uuid
        self.path = path
        self.original_filename = original_filename
        self.date = date or datetime(2026, 9, 5, 12, 34, 56)
        self.date_modified = date_modified


class _FakePhotosDB:
    def __init__(self, photos: list[_FakePhoto]) -> None:
        self._photos = photos

    def photos(self) -> list[_FakePhoto]:
        return list(self._photos)

    def get_photo(self, uuid: str) -> _FakePhoto | None:
        for p in self._photos:
            if p.uuid == uuid:
                return p
        return None


def _install_fake_osxphotos(
    monkeypatch: pytest.MonkeyPatch, photos: list[_FakePhoto]
) -> None:
    """Stub osxphotos.PhotosDB(dbfile=...) to return our fake."""
    mod = types.ModuleType("osxphotos")

    def _PhotosDB(dbfile: str) -> _FakePhotosDB:
        _ = dbfile
        return _FakePhotosDB(photos)

    mod.PhotosDB = _PhotosDB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "osxphotos", mod)


def test_list_files_yields_local_photos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Photos with a local path are emitted as FileRecords."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    original = tmp_path / "IMG_0001.HEIC"
    original.write_bytes(b"the-photo-bytes")
    photos = [
        _FakePhoto(
            uuid="AAAA-BBBB-CCCC-DDDD",
            path=str(original),
            original_filename="IMG_0001.HEIC",
            date=datetime(2026, 9, 5, 12, 34, 56),
            date_modified=datetime(2026, 9, 6, 10, 0, 0),
        ),
    ]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:personal",
        photos_library_path=library,
    )
    got = list(src.list_files())
    assert len(got) == 1
    rec = got[0]
    assert rec.source_id == "icloud:personal"
    assert rec.size == len(b"the-photo-bytes")
    assert rec.foreign_hash is None
    assert rec.cloud_file_id == "AAAA-BBBB-CCCC-DDDD"
    assert rec.etag.startswith("AAAA-BBBB-CCCC-DDDD:")
    # etag encodes date_modified iso — a rescan with the same edit history
    # yields an identical etag.
    assert "2026-09-06" in rec.etag
    assert rec.owner is None
    assert rec.is_shared is False
    # PosixPath collapses ``//`` to ``/`` — assert on the source-prefix
    # shape only.
    assert str(rec.path).startswith("iclouddrive:icloud:personal:")
    assert "IMG_0001.HEIC" in str(rec.path)
    assert src.stub_count == 0


def test_list_files_skips_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Photos with path=None (iCloud-only stubs) are NOT emitted."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    downloaded = tmp_path / "downloaded.HEIC"
    downloaded.write_bytes(b"abc")
    photos = [
        _FakePhoto(uuid="STUB-1", path=None, original_filename="stub-1.HEIC"),
        _FakePhoto(uuid="STUB-2", path=None, original_filename="stub-2.HEIC"),
        _FakePhoto(
            uuid="LOCAL-1",
            path=str(downloaded),
            original_filename="downloaded.HEIC",
        ),
    ]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["LOCAL-1"]
    assert src.stub_count == 2


def test_move_to_trash_raises(tmp_path: Path) -> None:
    """iCloud Photos is permanently read-only — trash is done via Photos.app."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
        is_read_only_scan=False,  # even with flag off, refuse
    )
    rec = FileRecord(
        path=Path("iclouddrive:icloud:x://IMG.HEIC"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="icloud:x",
        cloud_file_id="AAAA-BBBB",
    )
    with pytest.raises(SourceError) as exc:
        src.move_to_trash(rec)
    msg = str(exc.value).lower()
    assert "photos.app" in msg or "read-only" in msg


def test_check_drift_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged date_modified → no exception."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    original = tmp_path / "IMG.HEIC"
    original.write_bytes(b"x")
    dm = datetime(2026, 9, 6, 10, 0, 0)
    photos = [
        _FakePhoto(
            uuid="STABLE",
            path=str(original),
            original_filename="IMG.HEIC",
            date_modified=dm,
        )
    ]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
    )
    rec = FileRecord(
        path=Path("iclouddrive:icloud:x://IMG.HEIC"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="icloud:x",
        cloud_file_id="STABLE",
        etag=f"STABLE:{dm.isoformat()}",
    )
    src.check_drift(rec)  # must NOT raise


def test_check_drift_date_modified_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different date_modified → SourceDriftError."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    original = tmp_path / "IMG.HEIC"
    original.write_bytes(b"x")
    scan_dm = datetime(2026, 9, 6, 10, 0, 0)
    new_dm = datetime(2026, 9, 7, 11, 0, 0)
    photos = [
        _FakePhoto(
            uuid="EDITED",
            path=str(original),
            original_filename="IMG.HEIC",
            date_modified=new_dm,
        )
    ]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
    )
    rec = FileRecord(
        path=Path("iclouddrive:icloud:x://IMG.HEIC"),
        size=1,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="icloud:x",
        cloud_file_id="EDITED",
        etag=f"EDITED:{scan_dm.isoformat()}",
    )
    with pytest.raises(SourceDriftError):
        src.check_drift(rec)


def test_read_bytes_streams_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_bytes yields the actual local file's bytes in chunks."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    payload = b"hello iCloud photos" * 100
    original = tmp_path / "IMG.HEIC"
    original.write_bytes(payload)
    photos = [
        _FakePhoto(
            uuid="STREAM",
            path=str(original),
            original_filename="IMG.HEIC",
        )
    ]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
    )
    rec = FileRecord(
        path=Path("iclouddrive:icloud:x://IMG.HEIC"),
        size=len(payload),
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="icloud:x",
        cloud_file_id="STREAM",
    )
    data = b"".join(src.read_bytes(rec, chunk_size=64))
    assert data == payload


def test_library_missing_raises(tmp_path: Path) -> None:
    """Nonexistent library path surfaces a typed error at list_files() time."""
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=tmp_path / "nonexistent.photoslibrary",
    )
    with pytest.raises(SourceError):
        list(src.list_files())


def test_osxphotos_missing_raises_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing osxphotos dep surfaces as SourceError with install hint."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()

    # Ensure osxphotos is un-importable.
    monkeypatch.setitem(sys.modules, "osxphotos", None)  # type: ignore[assignment]
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
    )
    with pytest.raises(SourceError) as exc:
        list(src.list_files())
    msg = str(exc.value).lower()
    assert "osxphotos" in msg


def test_get_metadata_reflects_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    original = tmp_path / "IMG.HEIC"
    original.write_bytes(b"x")
    photos = [_FakePhoto(uuid="U1", path=str(original), original_filename="IMG.HEIC")]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
    )
    rec = next(iter(src.list_files()))
    meta = src.get_metadata(rec)
    assert meta.cloud_file_id == "U1"
    assert meta.is_shared is False
    assert meta.etag == rec.etag


def test_cloud_path_scheme_locked_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Virtual path SHOULD be ``iclouddrive:<account>://<filename>``."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    original = tmp_path / "foo.jpg"
    original.write_bytes(b"y")
    photos = [_FakePhoto(uuid="U2", path=str(original), original_filename="foo.jpg")]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:personal",
        photos_library_path=library,
    )
    rec = next(iter(src.list_files()))
    assert str(rec.path).startswith("iclouddrive:icloud:personal:")
    assert "foo.jpg" in str(rec.path)


def test_is_read_only_scan_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with is_read_only_scan=False, move_to_trash refuses."""
    library = tmp_path / "Photos Library.photoslibrary"
    library.mkdir()
    original = tmp_path / "IMG.HEIC"
    original.write_bytes(b"x")
    photos = [_FakePhoto(uuid="U3", path=str(original), original_filename="IMG.HEIC")]
    _install_fake_osxphotos(monkeypatch, photos)
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=library,
        is_read_only_scan=False,
    )
    rec = next(iter(src.list_files()))
    with pytest.raises(SourceError):
        src.move_to_trash(rec)


def test_upload_refused() -> None:
    """iCloud is never a migrate destination."""
    src = iCloudPhotosSource(
        account_id="icloud:x",
        photos_library_path=Path("/nonexistent"),
    )
    with pytest.raises(NotImplementedError):
        src.upload("dest/x", iter([b""]), 0)


# Silences unused-import warnings for MagicMock / Any (kept for future extension).
_ = MagicMock, Any
