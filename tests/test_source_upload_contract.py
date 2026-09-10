"""Source.upload — v0.5-a write protocol.

Every test mocks the provider client so no real network hop fires.  Local
raises NotImplementedError (stretch goal deferred).  The gdrive + onedrive
implementations validate path shape BEFORE any HTTP call, gate on
``is_read_only_scan``, and re-fetch the destination-side digest so the
returned :class:`UploadResult` can drive v0.5-b's verify step.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.sources.base import (
    SourceError,
    SourcePermissionError,
    UploadResult,
)
from duplicate_cleaner.sources.gdrive import GoogleDriveSource
from duplicate_cleaner.sources.local import LocalFileSystemSource
from duplicate_cleaner.sources.onedrive import OneDriveSource


def _one_shot(data: bytes) -> Iterator[bytes]:
    yield data


# ---------------------------------------------------------------------------
# LocalFileSystemSource.upload — deferred stretch goal.
# ---------------------------------------------------------------------------


def test_local_upload_raises_not_implemented(tmp_path: Path) -> None:
    """v0.5-a: migrating INTO local disk is not supported yet."""
    src = LocalFileSystemSource(roots=[tmp_path])
    with pytest.raises(NotImplementedError) as exc:
        src.upload("foo.pdf", _one_shot(b"bytes"), 5)
    assert "v0.5" in str(exc.value)


# ---------------------------------------------------------------------------
# GoogleDriveSource.upload
# ---------------------------------------------------------------------------


def _install_fake_media_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub googleapiclient.http.MediaIoBaseUpload so tests don't need the real dep."""

    class _FakeMediaIoBaseUpload:
        def __init__(
            self,
            fd: Any,
            *,
            mimetype: str,
            resumable: bool = False,
            chunksize: int = -1,
        ) -> None:
            self.fd = fd
            self.mimetype = mimetype
            self.resumable = resumable
            self.chunksize = chunksize

    fake_http_mod = types.ModuleType("googleapiclient.http")
    fake_http_mod.MediaIoBaseUpload = _FakeMediaIoBaseUpload  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("googleapiclient")
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_parent)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", fake_http_mod)


def test_gdrive_upload_calls_files_create_with_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small-file upload issues one ``files.create`` under the resolved parent id."""
    _install_fake_media_upload(monkeypatch)
    service = MagicMock()
    files_client = service.files.return_value
    # No existing "Photos" folder — the planner miss triggers a folder create.
    list_request = MagicMock()
    list_request.execute.return_value = {"files": []}
    files_client.list.return_value = list_request

    # Two create calls: one for the folder, then one for the file.  Then a
    # get for the re-fetch.  Distinguish via ``call_args_list`` in the test.
    create_folder_resp = {"id": "FOLDER_ID_1"}
    create_file_resp = {"id": "NEW_FILE_ID_1"}
    fetched_resp = {
        "id": "NEW_FILE_ID_1",
        "md5Checksum": "abcdef1234567890" * 2,
        "modifiedTime": "2026-09-10T12:00:00.000Z",
    }

    create_request = MagicMock()
    create_request.execute.side_effect = [create_folder_resp, create_file_resp]
    files_client.create.return_value = create_request

    get_request = MagicMock()
    get_request.execute.return_value = fetched_resp
    files_client.get.return_value = get_request

    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    payload = b"small file"
    result = src.upload("Photos/img.jpg", _one_shot(payload), len(payload))

    # Two create calls (folder + file).
    calls = files_client.create.call_args_list
    assert len(calls) == 2
    folder_call = calls[0]
    file_call = calls[1]
    assert folder_call.kwargs["body"]["mimeType"] == (
        "application/vnd.google-apps.folder"
    )
    assert folder_call.kwargs["body"]["name"] == "Photos"
    assert file_call.kwargs["body"]["name"] == "img.jpg"
    assert file_call.kwargs["body"]["parents"] == ["FOLDER_ID_1"]
    assert result.cloud_file_id == "NEW_FILE_ID_1"


def test_gdrive_upload_returns_md5_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-upload re-fetch pulls md5Checksum into UploadResult."""
    _install_fake_media_upload(monkeypatch)
    service = MagicMock()
    files_client = service.files.return_value
    list_request = MagicMock()
    list_request.execute.return_value = {"files": []}
    files_client.list.return_value = list_request
    create_request = MagicMock()
    create_request.execute.return_value = {"id": "NEW_ID_XX"}
    files_client.create.return_value = create_request
    md5_hex = "0123456789abcdef" * 2
    get_request = MagicMock()
    get_request.execute.return_value = {
        "id": "NEW_ID_XX",
        "md5Checksum": md5_hex,
        "modifiedTime": "2026-09-10T12:00:00.000Z",
    }
    files_client.get.return_value = get_request

    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: service,
    )
    result = src.upload("bar.bin", _one_shot(b"1234"), 4)
    assert isinstance(result, UploadResult)
    assert result.uploaded_hash_algo == "md5"
    assert result.uploaded_hash == md5_hex
    assert result.etag == "NEW_ID_XX:2026-09-10T12:00:00.000Z"


def test_gdrive_upload_requires_write_scope() -> None:
    """A source constructed with is_read_only_scan=True refuses upload."""
    service = MagicMock()
    src = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=True,
        service_factory=lambda _c: service,
    )
    with pytest.raises(SourcePermissionError):
        src.upload("foo.bin", _one_shot(b""), 0)


# ---------------------------------------------------------------------------
# OneDriveSource.upload
# ---------------------------------------------------------------------------


def test_onedrive_upload_small_file_via_put() -> None:
    """A small file uploads with a single PUT to the /content endpoint."""
    client = MagicMock()
    put_resp = MagicMock()
    put_resp.status_code = 201
    put_resp.raise_for_status.return_value = None
    put_resp.json.return_value = {"id": "SMALL_ID"}
    client.put.return_value = put_resp
    # Re-fetch after upload.
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.raise_for_status.return_value = None
    get_resp.json.return_value = {
        "id": "SMALL_ID",
        "file": {"hashes": {"sha256Hash": "ABCDEF" + "0" * 58}},
        "lastModifiedDateTime": "2026-09-10T12:00:00.000Z",
    }
    client.get.return_value = get_resp

    src = OneDriveSource(
        "onedrive:main",
        token_provider=lambda: "tok",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    payload = b"hi there"
    result = src.upload("Docs/hello.txt", _one_shot(payload), len(payload))
    put_args, _put_kwargs = client.put.call_args
    assert "/me/drive/root:/Docs/hello.txt:/content" in put_args[0]
    assert result.cloud_file_id == "SMALL_ID"


def test_onedrive_upload_large_file_via_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large file goes through createUploadSession + chunked PUT."""
    # Drop the small-upload threshold to force the large branch even for tiny
    # test payloads.  Chunk size is also small so the ``Content-Range`` math
    # exercises both refill + terminal-response paths.
    import duplicate_cleaner.sources.onedrive as od

    monkeypatch.setattr(od, "_ONEDRIVE_SMALL_UPLOAD_THRESHOLD_BYTES", 8)
    monkeypatch.setattr(od, "_ONEDRIVE_UPLOAD_CHUNK_SIZE_BYTES", 8)

    graph_client = MagicMock()
    # POST createUploadSession → uploadUrl.
    session_resp = MagicMock()
    session_resp.status_code = 200
    session_resp.raise_for_status.return_value = None
    session_resp.json.return_value = {
        "uploadUrl": "https://example-onedrive-upload.local/session/abc",
    }
    graph_client.post.return_value = session_resp
    # Post-upload re-fetch (via Graph GET) returns final metadata.
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.raise_for_status.return_value = None
    get_resp.json.return_value = {
        "id": "LARGE_ID",
        "file": {"hashes": {"sha256Hash": "CAFE" + "0" * 60}},
        "lastModifiedDateTime": "2026-09-10T12:00:00.000Z",
    }
    graph_client.get.return_value = get_resp

    # Chunk PUTs go through the unauth client.  Intercept via monkeypatch.
    put_calls: list[dict[str, Any]] = []

    class _UnauthClient:
        def __init__(self) -> None:
            self._closed = False

        def put(
            self, url: str, *, content: bytes, headers: dict[str, str]
        ) -> Any:
            put_calls.append(
                {"url": url, "size": len(content), "headers": dict(headers)}
            )
            resp = MagicMock()
            # Return 202 on intermediate chunks, 201 on the terminal chunk.
            # We estimate "terminal" as the last chunk in the file.
            resp.status_code = 201 if len(put_calls) >= 2 else 202
            resp.json.return_value = {"id": "LARGE_ID"}
            return resp

        def close(self) -> None:
            self._closed = True

    monkeypatch.setattr(od, "_build_unauth_http_client", _UnauthClient)

    src = OneDriveSource(
        "onedrive:main",
        token_provider=lambda: "tok",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: graph_client,
    )
    payload = b"0123456789ABCDEF"  # 16 bytes → 2 chunks of 8.
    result = src.upload("Big/thing.iso", _one_shot(payload), len(payload))

    # createUploadSession was called once, on the Graph client.
    assert graph_client.post.call_count == 1
    session_url = graph_client.post.call_args.args[0]
    assert "/me/drive/root:/Big/thing.iso:/createUploadSession" in session_url

    # Two chunked PUTs to the unauth session URL.
    assert len(put_calls) == 2
    assert put_calls[0]["headers"]["Content-Range"] == "bytes 0-7/16"
    assert put_calls[1]["headers"]["Content-Range"] == "bytes 8-15/16"
    assert result.cloud_file_id == "LARGE_ID"


def test_onedrive_upload_returns_sha256() -> None:
    """UploadResult.uploaded_hash_algo == 'sha256' with the lower-cased digest."""
    client = MagicMock()
    put_resp = MagicMock()
    put_resp.status_code = 201
    put_resp.raise_for_status.return_value = None
    put_resp.json.return_value = {"id": "OD01"}
    client.put.return_value = put_resp
    sha_hex_upper = "AA" * 32
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.raise_for_status.return_value = None
    get_resp.json.return_value = {
        "id": "OD01",
        "file": {"hashes": {"sha256Hash": sha_hex_upper}},
        "lastModifiedDateTime": "2026-09-10T12:00:00.000Z",
    }
    client.get.return_value = get_resp

    src = OneDriveSource(
        "onedrive:main",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: client,
    )
    result = src.upload("f.txt", _one_shot(b"x"), 1)
    assert result.uploaded_hash_algo == "sha256"
    assert result.uploaded_hash == sha_hex_upper.lower()


def test_onedrive_upload_requires_write_scope() -> None:
    """is_read_only_scan=True on OneDrive refuses upload too."""
    client = MagicMock()
    src = OneDriveSource(
        "onedrive:main",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=True,
        client_factory=lambda _tp: client,
    )
    with pytest.raises(SourcePermissionError):
        src.upload("foo.bin", _one_shot(b""), 0)


# ---------------------------------------------------------------------------
# Shared path-shape gate.
# ---------------------------------------------------------------------------


def test_upload_rejects_absolute_dest_path() -> None:
    """Absolute paths ('/foo/bar') fail the shape gate on BOTH providers.

    No HTTP call fires — the shape check runs before any client method.
    """
    gservice = MagicMock()
    # Any client method firing here would violate the invariant.
    gservice.files.return_value.create.side_effect = AssertionError(
        "create must not fire when the dest_path is refused up-front"
    )
    gsrc = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: gservice,
    )
    with pytest.raises(SourceError) as exc:
        gsrc.upload("/absolute/foo", _one_shot(b""), 0)
    assert "relative" in str(exc.value) or "leading" in str(exc.value)

    oclient = MagicMock()
    oclient.put.side_effect = AssertionError(
        "put must not fire when the dest_path is refused up-front"
    )
    osrc = OneDriveSource(
        "onedrive:main",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: oclient,
    )
    with pytest.raises(SourceError) as exc:
        osrc.upload("/absolute/foo", _one_shot(b""), 0)
    assert "relative" in str(exc.value) or "leading" in str(exc.value)


def test_upload_rejects_traversal_dest_path() -> None:
    """'../evil/foo' fails the shape gate on BOTH providers."""
    gservice = MagicMock()
    gservice.files.return_value.create.side_effect = AssertionError(
        "create must not fire when the dest_path is refused up-front"
    )
    gsrc = GoogleDriveSource(
        "gdrive:x",
        credentials=None,
        is_read_only_scan=False,
        service_factory=lambda _c: gservice,
    )
    with pytest.raises(SourceError) as exc:
        gsrc.upload("../evil/foo", _one_shot(b""), 0)
    assert "traversal" in str(exc.value)

    oclient = MagicMock()
    oclient.put.side_effect = AssertionError(
        "put must not fire when the dest_path is refused up-front"
    )
    osrc = OneDriveSource(
        "onedrive:main",
        token_provider=lambda: "t",
        account_user_id="me-oid",
        is_read_only_scan=False,
        client_factory=lambda _tp: oclient,
    )
    with pytest.raises(SourceError) as exc:
        osrc.upload("../evil/foo", _one_shot(b""), 0)
    assert "traversal" in str(exc.value)
