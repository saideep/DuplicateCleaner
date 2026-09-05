"""GoogleDriveSource — list, filter, read-bytes, retry, and read-only tripwire.

Every test mocks the Drive service factory so no real network is touched.
The retry tests inject a lightweight fake ``HttpError`` because the real
class from ``googleapiclient.errors`` may not be installed in every CI
environment.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.sources.base import SourceError, TrashedLocation
from duplicate_cleaner.sources.gdrive import GoogleDriveSource


def _mk_service(files_list_pages: list[dict[str, Any]]) -> MagicMock:
    """Build a mock Drive service whose files().list() paginates over ``files_list_pages``."""
    service = MagicMock()
    files_client = MagicMock()
    service.files.return_value = files_client

    pages_iter = iter(files_list_pages)

    def _list(**_kwargs: Any) -> MagicMock:
        request = MagicMock()
        page = next(pages_iter, {"files": []})
        request.execute.return_value = page
        return request

    files_client.list.side_effect = _list
    return service


def _drive_item(
    file_id: str,
    *,
    name: str = "file.pdf",
    size: int = 100,
    md5: str | None = "abcd" * 8,
    modified: str = "2026-09-05T12:34:56.000Z",
    mime: str = "application/pdf",
    shared: bool = False,
    trashed: bool = False,
    owner_email: str = "me@example.com",
    owner_me: bool = True,
    parents: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": file_id,
        "name": name,
        "modifiedTime": modified,
        "mimeType": mime,
        "shared": shared,
        "trashed": trashed,
        "owners": [{"emailAddress": owner_email, "me": owner_me}],
    }
    if md5 is not None:
        item["md5Checksum"] = md5
    if size is not None:
        item["size"] = str(size)
    if parents is not None:
        item["parents"] = parents
    return item


def test_list_files_produces_expected_records() -> None:
    page = {"files": [_drive_item("id1", name="a.pdf", size=42, md5="cafe" * 8)]}
    service = _mk_service([page])
    src = GoogleDriveSource(
        account_id="gdrive:personal",
        credentials=None,
        service_factory=lambda _c: service,
    )
    got = list(src.list_files())
    assert len(got) == 1
    rec = got[0]
    assert rec.source_id == "gdrive:personal"
    assert rec.size == 42
    assert rec.foreign_hash == "md5:" + "cafe" * 8
    assert rec.cloud_file_id == "id1"
    assert rec.etag.startswith("id1:")
    assert rec.owner == "me@example.com"
    assert rec.is_shared is False
    # Path normalises "//" to "/", so we check the source prefix only.
    assert str(rec.path).startswith("gdrive:personal:")


def test_google_native_docs_are_filtered_out() -> None:
    page = {
        "files": [
            _drive_item("d1", name="doc", mime="application/vnd.google-apps.document"),
            _drive_item(
                "s1", name="sheet", mime="application/vnd.google-apps.spreadsheet"
            ),
            _drive_item("keep", name="real.bin", mime="application/octet-stream"),
        ]
    }
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["keep"]


def test_trashed_items_are_filtered_out() -> None:
    page = {"files": [_drive_item("t", trashed=True), _drive_item("keep")]}
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["keep"]


def test_shared_files_yield_is_shared_true() -> None:
    page = {
        "files": [
            _drive_item("s1", shared=True),
            _drive_item("s2", owner_me=False, owner_email="other@x.com"),
            _drive_item("own", shared=False),
        ]
    }
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    got = {r.cloud_file_id: r for r in src.list_files()}
    assert got["s1"].is_shared is True
    assert got["s2"].is_shared is True
    assert got["own"].is_shared is False


def test_foreign_hash_is_md5_prefixed() -> None:
    page = {"files": [_drive_item("h", md5="ab" * 16)]}
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.foreign_hash == "md5:" + "ab" * 16


def test_missing_md5_yields_none_foreign_hash() -> None:
    page = {"files": [_drive_item("h", md5=None)]}
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.foreign_hash is None


def test_pagination_walks_all_pages() -> None:
    page1 = {"files": [_drive_item("a")], "nextPageToken": "tok1"}
    page2 = {"files": [_drive_item("b")]}
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page1, page2]),
    )
    ids = [r.cloud_file_id for r in src.list_files()]
    assert ids == ["a", "b"]


def test_move_to_trash_raises_when_read_only() -> None:
    page = {"files": [_drive_item("x")]}
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert src.is_read_only_scan is True
    with pytest.raises(PermissionError):
        src.move_to_trash(rec)


def test_move_to_trash_calls_update_with_trashed_true() -> None:
    page = {"files": [_drive_item("x")]}
    service = _mk_service([page])
    update_request = MagicMock()
    update_request.execute.return_value = {"id": "x", "trashed": True}
    service.files.return_value.update.return_value = update_request
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = next(iter(src.list_files()))
    loc = src.move_to_trash(rec)
    call = service.files.return_value.update.call_args
    assert call.kwargs["fileId"] == "x"
    assert call.kwargs["body"] == {"trashed": True}
    assert isinstance(loc, TrashedLocation)
    assert loc.source_id == "gdrive:x"
    assert loc.cloud_file_id == "x"
    assert loc.cloud_trash_id == "x"  # Drive keeps the same id after trashing


def test_restore_from_trash_calls_update_with_trashed_false() -> None:
    service = _mk_service([{"files": []}])
    update_request = MagicMock()
    update_request.execute.return_value = {"id": "abc", "trashed": False}
    service.files.return_value.update.return_value = update_request
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    loc = TrashedLocation(
        source_id="gdrive:x",
        original_path="gdrive:x://foo",
        cloud_file_id="abc",
        cloud_trash_id="abc",
    )
    src.restore_from_trash(loc)
    call = service.files.return_value.update.call_args
    assert call.kwargs["fileId"] == "abc"
    assert call.kwargs["body"] == {"trashed": False}


def test_restore_from_trash_rejects_wrong_source_id() -> None:
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([{"files": []}]),
    )
    loc = TrashedLocation(
        source_id="gdrive:other",
        original_path="gdrive:other://foo",
        cloud_file_id="id",
    )
    with pytest.raises(SourceError):
        src.restore_from_trash(loc)


def test_restore_from_trash_raises_not_found_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    service = MagicMock()
    update_request = MagicMock()
    update_request.execute.side_effect = HttpError(status=404)
    service.files.return_value.update.return_value = update_request
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    loc = TrashedLocation(
        source_id="gdrive:x",
        original_path="gdrive:x://foo",
        cloud_file_id="gone",
    )
    from duplicate_cleaner.sources.base import SourceNotFoundError

    with pytest.raises(SourceNotFoundError):
        src.restore_from_trash(loc)


def test_move_to_trash_retries_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    service = MagicMock()
    update_request = MagicMock()
    calls: list[int] = []

    def _execute() -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 2:
            raise HttpError(status=429)
        return {"id": "y", "trashed": True}

    update_request.execute.side_effect = _execute
    service.files.return_value.update.return_value = update_request
    # list_files uses a separate request; supply a stub page result too.
    list_request = MagicMock()
    list_request.execute.return_value = {"files": [_drive_item("y")]}
    service.files.return_value.list.return_value = list_request

    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = next(iter(src.list_files()))
    loc = src.move_to_trash(rec)
    assert loc.cloud_file_id == "y"
    assert len(calls) == 2  # one 429 then success


def test_move_to_trash_raises_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    service = MagicMock()
    update_request = MagicMock()
    update_request.execute.side_effect = HttpError(status=429)
    service.files.return_value.update.return_value = update_request
    list_request = MagicMock()
    list_request.execute.return_value = {"files": [_drive_item("z")]}
    service.files.return_value.list.return_value = list_request

    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    rec = next(iter(src.list_files()))
    from duplicate_cleaner.sources.base import SourceRateLimitError

    with pytest.raises(SourceRateLimitError):
        src.move_to_trash(rec)


def test_owner_missing_me_field_marks_shared() -> None:
    """B2: when Drive omits ``owners[0].me`` we must treat the file as shared."""
    page = {
        "files": [
            {
                "id": "no-me-field",
                "name": "shared.pdf",
                "modifiedTime": "2026-09-05T00:00:00.000Z",
                "mimeType": "application/pdf",
                "shared": False,
                "trashed": False,
                "size": "10",
                "owners": [{"emailAddress": "someone@else.com"}],
            }
        ]
    }
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.is_shared is True
    assert rec.owner == "someone@else.com"


def test_read_bytes_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """MediaIoBaseDownload is patched to write two chunks into the buffer."""
    fake_downloader_calls: list[int] = []
    payloads = [b"chunk-one", b"chunk-two"]

    class _FakeDownloader:
        def __init__(self, fd: Any, request: Any, chunksize: int) -> None:
            self._fd = fd
            self._i = 0
            fake_downloader_calls.append(chunksize)

        def next_chunk(self) -> tuple[MagicMock, bool]:
            if self._i >= len(payloads):
                return (MagicMock(), True)
            self._fd.write(payloads[self._i])
            self._i += 1
            done = self._i >= len(payloads)
            return (MagicMock(), done)

    fake_http_mod = types.ModuleType("googleapiclient.http")
    fake_http_mod.MediaIoBaseDownload = _FakeDownloader  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_parent)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", fake_http_mod)

    service = MagicMock()
    service.files.return_value.get_media.return_value = MagicMock()
    src = GoogleDriveSource(
        "gdrive:x", credentials=None, service_factory=lambda _c: service
    )
    from duplicate_cleaner.scan.walk import FileRecord

    rec = FileRecord(
        path=Path("gdrive:x://foo"),
        size=len(b"".join(payloads)),
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gdrive:x",
        cloud_file_id="cf1",
        etag="cf1:t",
    )
    chunks = list(src.read_bytes(rec, chunk_size=1234))
    assert b"".join(chunks) == b"chunk-onechunk-two"
    assert fake_downloader_calls == [1234]


def test_read_bytes_requires_cloud_file_id() -> None:
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([{"files": []}]),
    )
    from duplicate_cleaner.scan.walk import FileRecord

    rec = FileRecord(path=Path("x"), size=0, mtime=0.0, inode=0, dev=0, nlink=1)
    with pytest.raises(SourceError):
        list(src.read_bytes(rec))


def _install_fake_httperror(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Stub googleapiclient.errors.HttpError so the retry classifier fires."""

    class _FakeResp:
        def __init__(self, status: int) -> None:
            self.status = status

    class HttpError(Exception):
        def __init__(self, status: int, content: bytes = b"") -> None:
            super().__init__(f"HTTP {status}")
            self.resp = _FakeResp(status)
            self.content = content

    errors_mod = types.ModuleType("googleapiclient.errors")
    errors_mod.HttpError = HttpError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", errors_mod)
    return HttpError


def test_retry_on_429_backs_off_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    # Speed up tenacity by no-oping its sleep.
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    calls: list[int] = []
    page_success = {"files": [_drive_item("a")]}

    service = MagicMock()
    request = MagicMock()

    def _execute() -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 3:
            raise HttpError(status=429)
        return page_success

    request.execute.side_effect = _execute
    service.files.return_value.list.return_value = request

    src = GoogleDriveSource(
        "gdrive:x", credentials=None, service_factory=lambda _c: service
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["a"]
    assert len(calls) == 3  # two 429s then success


def test_retry_on_500_backs_off_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HttpError = _install_fake_httperror(monkeypatch)
    import tenacity  # type: ignore[import-untyped]

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)

    calls: list[int] = []
    page_success = {"files": [_drive_item("z")]}
    service = MagicMock()
    request = MagicMock()

    def _execute() -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 2:
            raise HttpError(status=503)
        return page_success

    request.execute.side_effect = _execute
    service.files.return_value.list.return_value = request

    src = GoogleDriveSource(
        "gdrive:x", credentials=None, service_factory=lambda _c: service
    )
    got = list(src.list_files())
    assert [r.cloud_file_id for r in got] == ["z"]
    assert len(calls) == 2


def test_parent_path_chain_is_built() -> None:
    """A file with a parent id resolves to `${parent_name}/${file_name}`."""
    page = {
        "files": [
            _drive_item("f1", name="doc.pdf", parents=["parentA"]),
        ]
    }
    service = _mk_service([page])
    # files.get for the parent chain:
    parent_get_request = MagicMock()
    parent_get_request.execute.return_value = {
        "id": "parentA",
        "name": "My Folder",
        "parents": [],
    }
    service.files.return_value.get.return_value = parent_get_request

    src = GoogleDriveSource(
        "gdrive:x", credentials=None, service_factory=lambda _c: service
    )
    got = list(src.list_files())
    assert str(got[0].path).endswith("My Folder/doc.pdf")


def test_get_metadata_reflects_record() -> None:
    page = {"files": [_drive_item("m1", shared=True)]}
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    meta = src.get_metadata(rec)
    assert meta.cloud_file_id == "m1"
    assert meta.is_shared is True
    assert meta.etag == rec.etag


def test_source_id_stamped_on_records() -> None:
    page = {"files": [_drive_item("x")]}
    src = GoogleDriveSource(
        "gdrive:my-drive-id",
        credentials=None,
        service_factory=lambda _c: _mk_service([page]),
    )
    rec = next(iter(src.list_files()))
    assert rec.source_id == "gdrive:my-drive-id"


def _unused_iter_helper() -> Iterator[bytes]:
    yield b""
